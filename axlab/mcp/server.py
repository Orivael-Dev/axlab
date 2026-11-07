"""AxLab MCP server: the fleet kill switch and the AxLab experiments over MCP.

Standard library only. It speaks MCP over stdio: newline-delimited JSON-RPC
2.0 on stdin/stdout, with logs on stderr only.

  python -m axlab.mcp            (or the ``axlab-mcp`` console script)

Tools

  axlab_fleet_status         effective level of the fleet and any scopes
  axlab_fleet_raise          raise a scope to L1-L4.  Raise only: lowering is
                             a signed release, a human step on the CLI,
                             never over MCP
  axlab_tracks               the research tracks in this checkout
  axlab_bench_slim_fabric    slim fabric run with the scripted model
  axlab_bench_kill_switch    one kill-switch experiment, --quick

The kill switch is the same ``.nodexloop/governance.json`` that the slim
fabric, AXIOM loop runs and the NodeXLoop extension read (``NXL_GOVERNANCE_FILE``
overrides it; the default is relative to the server's working directory).
An agent can pull the stop; only a person holding the release key can lower
it again.  As in ``axlab.governance.fleet_level``, this is not a security
boundary against code running as the same OS user.

The benchmarks never touch the network or a model.  They run as
subprocesses in a fresh temp directory, without the caller's keys or
governance file, so a benchmark cannot change the real fleet level and the
real level does not stop a benchmark.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from axlab import __version__
from axlab.governance import fleet_level as fl

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "experiments"

MAX_AGENTS = 100_000
MAX_LIVE = 500
BENCH_TIMEOUT_S = 300
KILL_SWITCH_EXPERIMENTS = ("footprint", "overhead", "propagation", "throughput", "halt")
_BENCH_ENV_DROP = ("AXLAB_MASTER_KEY", "AXIOM_MASTER_KEY", "NXL_RELEASE_KEY",
                   "NXL_GOVERNANCE_FILE")

INSTRUCTIONS = (
    "AxLab: Orivael's public agent-infrastructure experiments. "
    "axlab_fleet_raise is a kill switch for every agent run that reads the same "
    "governance file: L1 warn, L2 throttle, L3 suspend, L4 terminate. Use it to "
    "stop runaway or unsafe agent work, and always give a reason. It can only raise. "
    "Lowering is a signed human release on the command line, so never ask the user "
    "to lower it through you. The benchmark tools use a scripted model, no network."
)


class ToolError(Exception):
    """A tool-level failure: returned to the client as isError, not a protocol error."""


# ── argument helpers ─────────────────────────────────────────────────────────

def _check_keys(args: dict, allowed: set[str]) -> None:
    extra = set(args) - allowed
    if extra:
        raise ToolError(f"unknown argument(s): {', '.join(sorted(extra))}")


def _int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ToolError(f"{key} must be an integer")
    if not lo <= v <= hi:
        raise ToolError(f"{key} must be between {lo} and {hi}")
    return v


def _str(args: dict, key: str, default: str | None = None) -> str:
    v = args.get(key, default)
    if not isinstance(v, str):
        raise ToolError(f"{key} must be a string")
    return v


# ── tools ────────────────────────────────────────────────────────────────────

def _switch() -> fl.FleetSwitch:
    return fl.FleetSwitch(fl.default_path(), poll_s=0.0)


def tool_fleet_status(args: dict, ctx: dict) -> dict:
    _check_keys(args, {"scopes"})
    scopes = args.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ToolError("scopes must be a list of strings")
    try:
        for s in scopes:
            fl.check_scope(s)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    sw = _switch()
    snap = sw.snapshot()
    eff = sw.level(*scopes)
    return {
        "effective": fl.LEVELS[eff],
        "effective_level": eff,
        "checked_scopes": ["fleet", *[s for s in scopes if s != "fleet"]],
        "levels": snap["named"],
        "note": snap["note"],
        "file": snap["file"],
        "file_exists": Path(snap["file"]).exists(),
    }


def tool_fleet_raise(args: dict, ctx: dict) -> dict:
    _check_keys(args, {"scope", "level", "reason"})
    scope = _str(args, "scope", "fleet")
    reason = _str(args, "reason", "").strip()
    if not reason:
        raise ToolError("a reason is required; it goes into the receipt log")
    if "level" not in args:
        raise ToolError("level is required (1-4, L3, SUSPEND, ...)")
    try:
        fl.check_scope(scope)
        lv = fl.parse_level(args["level"])
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if lv == fl.NORMAL:
        raise ToolError("level 0 would lower the switch. Lowering is a signed human "
                        "release: python -m axlab.governance.fleet_level release ...")
    sw = _switch()
    before = sw.level(scope)
    try:
        after = sw.raise_level(scope, lv, by=f"mcp:{ctx.get('client', 'unknown')}",
                               reason=reason)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    except TimeoutError as exc:
        raise ToolError(f"governance file is locked: {exc}") from None
    return {
        "scope": scope,
        "from": fl.LEVELS[before],
        "to": fl.LEVELS[after],
        "file": str(sw.path),
        "receipts": str(sw.path.with_name(fl.RECEIPTS_NAME)),
        "release": (f"python -m axlab.governance.fleet_level release {scope} 0 "
                    f"--reason \"...\"  (a person with the release key)"),
    }


def _readme_summary(readme: Path) -> tuple[str, str]:
    title, para = readme.parent.name, ""
    lines = readme.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() == "" and body:
                    break
                if nxt.strip() and not nxt.startswith("#"):
                    body.append(nxt.strip())
            para = " ".join(body)
            break
    return title, para


def tool_tracks(args: dict, ctx: dict) -> dict:
    _check_keys(args, set())
    if not EXPERIMENTS.is_dir():
        raise ToolError("no experiments/ folder next to this axlab package "
                        "(install from a clone with pip install -e .)")
    tracks = []
    for d in sorted(p for p in EXPERIMENTS.iterdir() if p.is_dir()):
        readme = d / "README.md"
        title, para = _readme_summary(readme) if readme.exists() else (d.name, "")
        tracks.append({
            "track": d.name,
            "title": title,
            "summary": para,
            "runnable": (d / "bench.py").exists(),
            "results": sorted(p.name for p in (d / "results").glob("*.json"))
                       if (d / "results").is_dir() else [],
        })
    return {"tracks": tracks}


def _run_bench(cmd: list[str], workdir: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in _BENCH_ENV_DROP}
    try:
        r = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True,
                           timeout=BENCH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError(f"benchmark did not finish within {BENCH_TIMEOUT_S} s") from None
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-15:]
        raise ToolError("benchmark failed:\n" + "\n".join(tail))


def _bench_script(track: str) -> Path:
    p = EXPERIMENTS / track / "bench.py"
    if not p.exists():
        raise ToolError(f"{p} not found (install from a clone with pip install -e .)")
    return p


def tool_bench_slim_fabric(args: dict, ctx: dict) -> dict:
    _check_keys(args, {"agents", "templates", "live", "seed"})
    agents = _int(args, "agents", 1000, 1, MAX_AGENTS)
    templates = _int(args, "templates", 10, 1, 1000)
    live = _int(args, "live", 50, 0, MAX_LIVE)
    seed = _int(args, "seed", 0, 0, 2**31 - 1)
    if templates > agents:
        raise ToolError("templates cannot exceed agents")
    bench = _bench_script("slim_fabric")
    work = Path(tempfile.mkdtemp(prefix="axlab_mcp_"))
    out = work / "slim_fabric.json"
    _run_bench([sys.executable, str(bench), "--agents", str(agents),
                "--templates", str(templates), "--live", str(live), "--backend", "fake",
                "--seed", str(seed), "--db", str(work / "store.db"), "--out", str(out)],
               work)
    report = json.loads(out.read_text(encoding="utf-8"))
    return {"workload": "scripted model (50 ms/turn), simulated 200 ms tools, no network",
            "result_file": str(out), "report": report}


def tool_bench_kill_switch(args: dict, ctx: dict) -> dict:
    _check_keys(args, {"experiment", "seed"})
    exp = _str(args, "experiment", "overhead")
    if exp not in KILL_SWITCH_EXPERIMENTS:
        raise ToolError(f"experiment must be one of {', '.join(KILL_SWITCH_EXPERIMENTS)}")
    seed = _int(args, "seed", 7, 0, 2**31 - 1)
    bench = _bench_script("fleet_kill_switch")
    work = Path(tempfile.mkdtemp(prefix="axlab_mcp_"))
    _run_bench([sys.executable, str(bench), exp, "--quick", "--seed", str(seed),
                "--results-dir", str(work)], work)
    out = work / f"{exp}.json"
    return {"workload": "quick mode, simulated model, no network",
            "result_file": str(out), "report": json.loads(out.read_text(encoding="utf-8"))}


_LEVEL_SCHEMA = {"description": "1-4, or L1..L4, WARNING, THROTTLE, SUSPEND, TERMINATE",
                 "anyOf": [{"type": "integer", "minimum": 1, "maximum": 4},
                           {"type": "string"}]}
_SCOPE_DESC = "fleet | loop:<graph name> | run:<run id> | fabric:<name> | agent:<id>"

TOOLS: dict[str, tuple[Callable[[dict, dict], dict], dict]] = {
    "axlab_fleet_status": (tool_fleet_status, {
        "title": "Fleet kill switch: status",
        "description": ("Effective kill-switch level for the fleet, plus any scopes you name "
                        "(the highest one wins). Read-only."),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "scopes": {"type": "array", "items": {"type": "string"}, "description": _SCOPE_DESC}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    }),
    "axlab_fleet_raise": (tool_fleet_raise, {
        "title": "Fleet kill switch: raise",
        "description": ("Raise the kill switch for a scope: L1 warn, L2 throttle (one model slot, "
                        "side effects need a human), L3 suspend (runs checkpoint, can resume), "
                        "L4 terminate (in-flight work dropped, runs cannot resume). Raise only; "
                        "a person lowers it with a signed release. Written to a chained receipt log."),
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "required": ["level", "reason"], "properties": {
            "scope": {"type": "string", "default": "fleet", "description": _SCOPE_DESC},
            "level": _LEVEL_SCHEMA,
            "reason": {"type": "string", "description": "why; recorded in the receipt"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": False, "openWorldHint": False},
    }),
    "axlab_tracks": (tool_tracks, {
        "title": "AxLab research tracks",
        "description": "The research tracks in this checkout: title, summary, runnable, result files.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    }),
    "axlab_bench_slim_fabric": (tool_bench_slim_fabric, {
        "title": "Benchmark: slim agent fabric",
        "description": ("Build N agents, park half, time routing, then push `live` events through "
                        "the runtime with a scripted model. Returns the full JSON report "
                        "(memory, routing p50/p95, tasks, tokens). No network, no model."),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "agents": {"type": "integer", "minimum": 1, "maximum": MAX_AGENTS, "default": 1000},
            "templates": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 10},
            "live": {"type": "integer", "minimum": 0, "maximum": MAX_LIVE, "default": 50},
            "seed": {"type": "integer", "minimum": 0, "default": 0}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    }),
    "axlab_bench_kill_switch": (tool_bench_kill_switch, {
        "title": "Benchmark: fleet kill switch",
        "description": ("One kill-switch experiment in quick mode: footprint, overhead (ns per "
                        "check), propagation (raise in one process, seen in another), throughput "
                        "(fleet cost of the switch), halt (tokens spent after an L3/L4)."),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "experiment": {"type": "string", "enum": list(KILL_SWITCH_EXPERIMENTS),
                           "default": "overhead"},
            "seed": {"type": "integer", "minimum": 0, "default": 7}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    }),
}


# ── JSON-RPC / MCP ───────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[axlab-mcp] {msg}", file=sys.stderr, flush=True)


class Server:
    def __init__(self, write: Callable[[dict], None]) -> None:
        self._write = write
        self.ctx: dict[str, Any] = {"client": "unknown"}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="axlab-tool")

    def tools_list(self) -> list[dict]:
        return [{"name": name, **meta} for name, (_, meta) in TOOLS.items()]

    def call_tool(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOLS[name][0]
        if not isinstance(args, dict):
            return _tool_result({"error": "arguments must be an object"}, True)
        try:
            return _tool_result(fn(args, self.ctx), False)
        except ToolError as exc:
            return _tool_result({"error": str(exc)}, True)
        except Exception as exc:   # a tool bug must not kill the server
            _log(f"{name} crashed: {type(exc).__name__}: {exc}")
            return _tool_result({"error": f"{type(exc).__name__}: {exc}"}, True)

    def handle(self, msg: Any) -> dict | None:
        """One incoming message -> the response, or None (notification / async)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600,
                          "invalid request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        is_note = "id" not in msg
        if method == "initialize":
            info = params.get("clientInfo") or {}
            self.ctx["client"] = str(info.get("name") or "unknown")[:64]
            asked = params.get("protocolVersion")
            ver = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _ok(mid, {"protocolVersion": ver,
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "axlab", "title": "AxLab",
                                            "version": __version__},
                             "instructions": INSTRUCTIONS})
        if is_note:
            return None          # notifications/initialized, cancelled, ... : nothing to send
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": self.tools_list()})
        if method == "tools/call":
            if params.get("name") not in TOOLS:
                return _error(mid, -32602, f"unknown tool: {params.get('name')!r}")
            # Tools run off the read loop so a long benchmark never delays a
            # kill-switch raise.
            self._pool.submit(lambda: self._write(_ok(mid, self.call_tool(params))))
            return None
        return _error(mid, -32601, f"method not found: {method}")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)


def _ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_result(data: dict, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}],
            "structuredContent": data, "isError": is_error}


def serve(stdin=None, stdout=None) -> int:
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    lock = threading.Lock()

    def write(obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with lock:
            out.write(data)
            out.flush()

    server = Server(write)
    _log(f"v{__version__} ready; governance file {fl.default_path()}")
    try:
        for raw in inp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                write(_error(None, -32700, "parse error"))
                continue
            if isinstance(msg, list):
                write(_error(None, -32600, "batches are not supported"))
                continue
            resp = server.handle(msg)
            if resp is not None:
                write(resp)
    finally:
        server.shutdown()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    sys.exit(main())
