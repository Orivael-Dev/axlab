"""AxLab MCP server, driven over real stdio like an MCP client would."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KEY = bytes(range(32))


class Client:
    def __init__(self, gov: Path):
        env = dict(os.environ, NXL_GOVERNANCE_FILE=str(gov), NXL_RELEASE_KEY=KEY.hex(),
                   PYTHONPATH=str(REPO))
        self.p = subprocess.Popen([sys.executable, "-m", "axlab.mcp"], cwd=gov.parents[1], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.order: list = []
        threading.Thread(target=self._read, daemon=True).start()
        self._id = 0

    def _read(self):
        for line in self.p.stdout:
            msg = json.loads(line)
            self.order.append(msg.get("id"))
            self.q.put(msg)

    def send(self, method, params=None, *, notify=False, raw=None):
        if raw is not None:
            self.p.stdin.write(raw)
        else:
            msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notify:
                self._id += 1
                msg["id"] = self._id
            self.p.stdin.write(json.dumps(msg).encode() + b"\n")
        self.p.stdin.flush()
        return self._id

    def recv(self, mid, timeout=120):
        pending = []
        try:
            while True:
                m = self.q.get(timeout=timeout)
                if m.get("id") == mid:
                    return m
                pending.append(m)
        finally:
            for m in pending:
                self.q.put(m)

    def call(self, method, params=None, timeout=120):
        return self.recv(self.send(method, params), timeout)

    def tool(self, name, args=None, timeout=120):
        r = self.call("tools/call", {"name": name, "arguments": args or {}}, timeout)
        res = r["result"]
        assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
        return res["isError"], res["structuredContent"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)


@pytest.fixture
def mcp(tmp_path):
    c = Client(tmp_path / ".nodexloop" / "governance.json")
    init = c.call("initialize", {"protocolVersion": "2025-06-18",
                                 "clientInfo": {"name": "test-client", "version": "1"},
                                 "capabilities": {}})
    c.send("notifications/initialized", notify=True)
    c.init = init
    yield c
    c.close()


def test_initialize_and_list(mcp):
    r = mcp.init["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert r["serverInfo"]["name"] == "axlab" and "tools" in r["capabilities"]
    tools = {t["name"]: t for t in mcp.call("tools/list")["result"]["tools"]}
    assert set(tools) == {"axlab_fleet_status", "axlab_fleet_raise", "axlab_tracks",
                          "axlab_bench_slim_fabric", "axlab_bench_kill_switch"}
    assert tools["axlab_fleet_raise"]["annotations"]["destructiveHint"] is True
    assert all(t["inputSchema"]["type"] == "object" for t in tools.values())


def test_unknown_protocol_version_gets_ours(tmp_path):
    c = Client(tmp_path / "x" / "g.json")
    try:
        r = c.call("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        assert r["result"]["protocolVersion"] == "2025-06-18"
    finally:
        c.close()


def test_status_missing_file_is_normal(mcp):
    err, data = mcp.tool("axlab_fleet_status", {"scopes": ["run:x"]})
    assert not err and data["effective"] == "NORMAL" and data["file_exists"] is False


def test_raise_is_raise_only_and_receipted(mcp, tmp_path):
    err, data = mcp.tool("axlab_fleet_raise", {"level": "L3", "reason": "runaway loop"})
    assert not err and data["to"] == "L3 SUSPEND" and data["from"] == "NORMAL"
    assert mcp.tool("axlab_fleet_status")[1]["effective"] == "L3 SUSPEND"

    for bad in ({"level": 2, "reason": "lower"},          # a raise cannot go down
                {"level": 0, "reason": "release"},        # level 0 is a release
                {"level": 4},                             # reason required
                {"level": 4, "reason": " "},
                {"level": "L9", "reason": "x"},
                {"scope": "everything", "level": 4, "reason": "x"},
                {"level": 4, "reason": "x", "extra": 1}):
        err, data = mcp.tool("axlab_fleet_raise", bad)
        assert err and data["error"], bad

    err, data = mcp.tool("axlab_fleet_raise", {"scope": "run:abc", "level": 4, "reason": "kill"})
    assert not err and data["to"] == "L4 TERMINATE"
    rec = [json.loads(x) for x in
           (tmp_path / ".nodexloop" / "governance-receipts.jsonl").read_text().splitlines()]
    assert [r["by"] for r in rec] == ["mcp:test-client"] * 2
    assert [r["reason"] for r in rec] == ["runaway loop", "kill"]


def test_only_a_signed_release_lowers_it(mcp, tmp_path):
    from axlab.governance.fleet_level import FleetSwitch
    mcp.tool("axlab_fleet_raise", {"level": 4, "reason": "drill"})
    FleetSwitch(tmp_path / ".nodexloop" / "governance.json", key=KEY).release(
        "fleet", 0, by="person", reason="drill over")
    assert mcp.tool("axlab_fleet_status")[1]["effective"] == "NORMAL"


def test_protocol_errors(mcp):
    assert mcp.call("tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert mcp.call("no/such/method")["error"]["code"] == -32601
    assert mcp.call("ping")["result"] == {}
    mcp.send(None, raw=b"{not json\n")
    assert mcp.recv(None)["error"]["code"] == -32700


def test_tracks(mcp):
    err, data = mcp.tool("axlab_tracks")
    names = {t["track"]: t for t in data["tracks"]}
    assert not err and {"slim_fabric", "fleet_kill_switch"} <= set(names)
    assert names["fleet_kill_switch"]["runnable"] and "halt.json" in names["fleet_kill_switch"]["results"]


def test_bench_slim_fabric_is_isolated_from_the_real_switch(mcp, tmp_path):
    mcp.tool("axlab_fleet_raise", {"level": 4, "reason": "fleet stopped"})
    gov = tmp_path / ".nodexloop" / "governance.json"
    before = gov.read_bytes()
    err, data = mcp.tool("axlab_bench_slim_fabric", {"agents": 200, "templates": 5, "live": 10})
    assert not err, data
    live = data["report"]["live"]
    assert live["tasks_completed"] > 0 and live["errors"] == 0
    assert data["report"]["build"]["agents"] == 200
    assert gov.read_bytes() == before                 # the bench never wrote the real file
    err, data = mcp.tool("axlab_bench_slim_fabric", {"agents": 10**9})
    assert err and "between" in data["error"]


def test_bench_kill_switch_does_not_touch_published_results(mcp):
    published = REPO / "experiments" / "fleet_kill_switch" / "results" / "overhead.json"
    before = published.read_bytes()
    err, data = mcp.tool("axlab_bench_kill_switch", {"experiment": "overhead"})
    assert not err, data
    assert data["report"]["experiment"] == "overhead" and data["report"]["quick"] is True
    assert published.read_bytes() == before
    err, data = mcp.tool("axlab_bench_kill_switch", {"experiment": "live"})   # needs a model
    assert err


def test_a_raise_is_not_stuck_behind_a_benchmark(mcp):
    bench = mcp.send("tools/call", {"name": "axlab_bench_slim_fabric",
                                    "arguments": {"agents": 2000, "live": 100}})
    stop = mcp.send("tools/call", {"name": "axlab_fleet_raise",
                                   "arguments": {"level": 3, "reason": "stop"}})
    assert mcp.recv(stop)["result"]["isError"] is False
    assert mcp.recv(bench)["result"]["isError"] is False
    assert mcp.order.index(stop) < mcp.order.index(bench)
