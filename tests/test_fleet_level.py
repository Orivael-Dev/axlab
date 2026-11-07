"""Fleet kill switch — the switch itself and its hooks in the slim fabric."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axlab.governance.fleet_level import (  # noqa: E402
    LEVELS, NORMAL, SUSPEND, TERMINATE, THROTTLE, WARNING, FleetSwitch, canonical,
    evaluate, main, parse_level,
)

KEY = bytes(range(32))


@pytest.fixture(autouse=True)
def no_home_key(monkeypatch):
    # Never read or create ~/.nodexloop/release.key from a test.
    monkeypatch.setenv("NXL_RELEASE_KEY", KEY.hex())
    monkeypatch.delenv("NXL_GOVERNANCE_FILE", raising=False)
    monkeypatch.setenv("AXLAB_MASTER_KEY", "test" + "0" * 60)


def _sw(tmp_path, **kw):
    kw.setdefault("poll_s", 0.0)
    return FleetSwitch(tmp_path / "governance.json", key=KEY, **kw)


# ── the switch ───────────────────────────────────────────────────────────────

def test_missing_file_is_normal(tmp_path):
    assert _sw(tmp_path).level() == NORMAL


@pytest.mark.parametrize("raw,want", [(4, 4), ("4", 4), ("L3", 3), ("l2 throttle", 2),
                                      ("TERMINATE", 4), ("L1 WARNING", 1), ("NORMAL", 0)])
def test_parse_level(raw, want):
    assert parse_level(raw) == want


@pytest.mark.parametrize("raw", ["L5", 9, -1, "STOP", True])
def test_parse_level_rejects(raw):
    with pytest.raises(ValueError):
        parse_level(raw)


def test_raise_needs_no_key_and_only_goes_up(tmp_path):
    keyless = FleetSwitch(tmp_path / "governance.json", poll_s=0.0, key=None)
    # A keyless writer on a machine with no release key at all.
    import os
    os.environ.pop("NXL_RELEASE_KEY")
    try:
        assert keyless.raise_level("fleet", "L2", by="t", reason="noisy") == THROTTLE
        with pytest.raises(ValueError, match="only goes up"):
            keyless.raise_level("fleet", 1, by="t")
        with pytest.raises(ValueError):
            keyless.raise_level("fleet", 2, by="t")
        assert keyless.raise_level("fleet", 4, by="t") == TERMINATE
    finally:
        os.environ["NXL_RELEASE_KEY"] = KEY.hex()


def test_scope_is_max_of_fleet_and_own(tmp_path):
    sw = _sw(tmp_path)
    sw.raise_level("run:abc", 3, by="t")
    assert sw.level("run:abc") == SUSPEND
    assert sw.level("run:other") == NORMAL
    assert sw.level() == NORMAL
    sw.raise_level("fleet", 1, by="t")
    assert sw.level("run:other") == WARNING
    assert sw.level("run:abc") == SUSPEND


def test_bad_scope_refused(tmp_path):
    with pytest.raises(ValueError, match="bad scope"):
        _sw(tmp_path).raise_level("everything", 4, by="t")


def test_release_lowers_and_acks_raises(tmp_path):
    sw = _sw(tmp_path)
    sw.raise_level("fleet", 4, by="ops", reason="runaway")
    with pytest.raises(ValueError, match="needs a reason"):
        sw.release("fleet", 0, by="tony", reason=" ")
    assert sw.release("fleet", 0, by="tony", reason="fixed") == NORMAL
    doc = json.loads((tmp_path / "governance.json").read_text())
    assert doc["raises"] == []                    # acknowledged raises are pruned
    # A raise after the release still counts.
    assert sw.raise_level("fleet", 3, by="ops") == SUSPEND


def test_release_can_hold_a_baseline(tmp_path):
    sw = _sw(tmp_path)
    sw.release("loop:nightly", 2, by="tony", reason="watch it")
    assert sw.level("loop:nightly") == THROTTLE
    with pytest.raises(ValueError):               # a raise cannot undo a signed hold
        sw.raise_level("loop:nightly", 1, by="tool")
    assert sw.level("loop:nightly") == THROTTLE


def test_release_with_the_wrong_key_refused(tmp_path):
    _sw(tmp_path).release("fleet", 1, by="tony", reason="x")
    with pytest.raises(PermissionError):
        FleetSwitch(tmp_path / "governance.json", key=b"\x01" * 32).release(
            "fleet", 0, by="mallory", reason="x")


def test_tampered_release_fails_closed(tmp_path):
    sw = _sw(tmp_path)
    sw.raise_level("fleet", 4, by="ops")
    sw.release("fleet", 0, by="tony", reason="fixed")
    p = tmp_path / "governance.json"
    doc = json.loads(p.read_text())
    doc["base"]["levels"] = {}
    doc["base"]["acks"] = {"fleet": 999}          # try to pre-acknowledge future raises
    p.write_text(json.dumps(doc))
    sw.refresh()
    assert sw.level() == SUSPEND and "does not verify" in sw.note


def test_release_without_key_here_fails_closed(tmp_path):
    _sw(tmp_path).release("fleet", 0, by="tony", reason="x")
    levels, note = evaluate(json.loads((tmp_path / "governance.json").read_text()), None)
    assert levels["fleet"] == SUSPEND and "no release key" in note


def test_garbled_file_fails_closed_and_a_raise_still_lands(tmp_path):
    p = tmp_path / "governance.json"
    p.write_text("{not json")
    sw = _sw(tmp_path)
    assert sw.level() == SUSPEND
    assert sw.raise_level("fleet", 4, by="ops") == TERMINATE


def test_receipts_are_chained(tmp_path):
    sw = _sw(tmp_path)
    sw.raise_level("fleet", 2, by="a", reason="r1")
    sw.raise_level("fleet", 4, by="b", reason="r2")
    sw.release("fleet", 0, by="c", reason="r3")
    lines = (tmp_path / "governance-receipts.jsonl").read_bytes().splitlines()
    assert len(lines) == 3
    recs = [json.loads(x) for x in lines]
    assert [r["op"] for r in recs] == ["raise", "raise", "release"]
    assert recs[0]["prev"] == ""
    for prev, rec in zip(lines, recs[1:]):
        assert rec["prev"] == hashlib.sha256(prev).hexdigest()
    assert "mac" in recs[2] and recs[1]["to"] == "L4 TERMINATE"


def test_poll_interval_caches_between_stats(tmp_path):
    sw = _sw(tmp_path, poll_s=60.0)
    assert sw.level() == NORMAL
    _sw(tmp_path).raise_level("fleet", 4, by="ops")
    assert sw.level() == NORMAL                   # inside the poll window: cached
    sw.refresh()
    assert sw.level() == TERMINATE


def test_level_check_is_cheap(tmp_path):
    sw = _sw(tmp_path, poll_s=0.05)
    sw.raise_level("run:x", 1, by="t")
    sw.level("loop:a", "run:x")
    n = 200_000
    t0 = time.perf_counter()
    for _ in range(n):
        sw.level("loop:a", "run:x")
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    assert per_call_us < 5, per_call_us           # ~0.3 us on a desktop


def test_canonical_matches_js_canonical_json():
    # Same vector the NodeXLoop extension asserts for its canonical JSON.
    assert canonical({"b": 1, "a": {"d": [1, "é"], "c": None}}) == \
        '{"a":{"c":null,"d":[1,"é"]},"b":1}'.encode("utf-8")


def test_cli_status_and_raise(tmp_path, capsys):
    f = str(tmp_path / "governance.json")
    assert main(["--file", f, "raise", "fleet", "L3", "--reason", "drill", "--by", "t"]) == 0
    assert json.loads(capsys.readouterr().out)["named"]["fleet"] == "L3 SUSPEND"
    assert main(["--file", f, "raise", "fleet", "1", "--by", "t"]) == 2
    assert main(["--file", f, "release", "fleet", "0", "--reason", "drill over", "--by", "t"]) == 0


# ── slim fabric ──────────────────────────────────────────────────────────────

def _fabric(tmp_path, fn, tools=None, slots=2, n=4):
    for mod in list(sys.modules):
        if mod.startswith(("axlab.fabric", "axlab.signing")):
            sys.modules.pop(mod, None)
    from axlab.fabric.agent_store import ContinuationStore
    from axlab.fabric.runtime import SlimFabricRuntime
    from axlab.fabric.slim_pool import AgentTemplate, SlimAgentPool
    from axlab.fabric.backends import BackendResult

    class B:
        name, model = "scripted", "t"
        live = peak = 0

        def generate(self, *, system, prompt, max_output_tokens, timeout_s=60.0):
            B.live += 1
            B.peak = max(B.peak, B.live)
            try:
                text = fn(prompt)
            finally:
                B.live -= 1
            return BackendResult(text=text, input_tokens=1, output_tokens=1, latency_ms=1,
                                 backend="scripted", model="t")

    pool = SlimAgentPool(n)
    pool.add_template(AgentTemplate(name="ops", role="ops", wake_conditions=["refund"],
                                    system_prompt="x", tool_permissions=["lookup"]).sign())
    pool.spawn("ops", n)
    rt = SlimFabricRuntime(pool, ContinuationStore(), B(), tools=tools or {},
                           model_slots=slots, switch=_sw(tmp_path, poll_s=0.005), scope="t")
    return rt, pool, B


def test_fabric_throttle_runs_one_slot(tmp_path):
    def slow(p):
        time.sleep(0.02)
        return "DONE"

    rt, pool, B = _fabric(tmp_path, slow, slots=4)
    _sw(tmp_path).raise_level("fabric:t", 2, by="ops")
    rt.submit("refund", k=4)
    stats = asyncio.run(rt.run_until_idle())
    assert stats.steps == 4 and B.peak == 1 and stats.halted == ""


def test_fabric_suspend_holds_then_resumes(tmp_path):
    from axlab.fabric.slim_pool import AgentState

    async def lookup(a):
        _sw(tmp_path).raise_level("fleet", 3, by="ops", reason="drill")
        await asyncio.sleep(0.02)
        return "ok"

    rt, pool, B = _fabric(tmp_path, lambda p: "DONE" if "[tool " in p else "TOOL lookup {}",
                          tools={"lookup": lookup}, n=2)

    async def go():
        rt.submit("refund", k=2)
        first = await rt.run_until_idle()
        assert first.halted == "L3 SUSPEND"
        held = pool.count_by_state()
        assert held["READY"] == 2 and held["WAITING_TOOL"] == 0   # tools finished
        steps_at_hold = first.steps
        _sw(tmp_path).release("fleet", 0, by="tony", reason="over")
        second = await rt.run_until_idle()
        assert second.halted == "" and second.steps == steps_at_hold + 2
        assert pool.count_by_state()["DORMANT"] == 2

    asyncio.run(go())


def test_fabric_terminate_archives_and_refuses(tmp_path):
    async def lookup(a):
        _sw(tmp_path).raise_level("fleet", 4, by="ops", reason="kill")
        await asyncio.sleep(5)                    # cancelled by the stop, not awaited
        return "never"

    rt, pool, B = _fabric(tmp_path, lambda p: "TOOL lookup {}", tools={"lookup": lookup}, n=3)
    rt.submit("refund", k=3)
    t0 = time.perf_counter()
    stats = asyncio.run(rt.run_until_idle())
    assert time.perf_counter() - t0 < 2
    assert stats.halted == "L4 TERMINATE" and stats.archived == 3
    assert pool.count_by_state()["ARCHIVED"] == 3
    assert rt.submit("refund", k=1) == [] and rt.stats.refused_events == 1


def test_fabric_without_switch_unchanged(tmp_path):
    rt, pool, B = _fabric(tmp_path, lambda p: "DONE")
    rt.switch = None
    rt.submit("refund", k=2)
    assert asyncio.run(rt.run_until_idle()).steps == 2



def test_fabric_obeys_the_switch_by_default(tmp_path, monkeypatch):
    for mod in list(sys.modules):
        if mod.startswith(("axlab.fabric", "axlab.signing")):
            sys.modules.pop(mod, None)
    from axlab.fabric.agent_store import ContinuationStore
    from axlab.fabric.runtime import SlimFabricRuntime
    from axlab.fabric.slim_pool import AgentTemplate, SlimAgentPool

    pool = SlimAgentPool(2)
    pool.add_template(AgentTemplate(name="ops", role="ops", wake_conditions=["refund"],
                                    system_prompt="x").sign())
    pool.spawn("ops", 2)
    monkeypatch.chdir(tmp_path)                         # default: ./.nodexloop/governance.json
    rt = SlimFabricRuntime(pool, ContinuationStore(), backend=None)
    assert rt.switch is not None and rt.switch.path == Path(".nodexloop/governance.json")
    FleetSwitch(tmp_path / ".nodexloop" / "governance.json", key=KEY).raise_level("fleet", 4, by="ops")
    rt.switch.refresh()
    assert rt.submit("refund", k=1) == [] and rt.stats.refused_events == 1
    assert SlimFabricRuntime(pool, ContinuationStore(), backend=None, switch=False).switch is None
