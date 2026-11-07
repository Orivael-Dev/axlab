"""Fleet kill switch benchmark — cost, reach, and what a stop saves.

Experiments (each writes results/<name>.json with the host it ran on):

  footprint    what a small device pays: import time, heap of the module and
               of one switch, third-party deps, file and receipt sizes.

  overhead     ns per FleetSwitch.level() — cached / uncached, file absent /
               present, 1 vs 3 scopes.
  propagation  raise in one process -> seen in another, at poll 10/50/100 ms;
               plus what a raise costs the writer.
  throughput   SlimFabricRuntime, N agents x T turns, scripted model: switch
               off / on (no file) / on (file at L1).  Completions/s and RSS.
  halt         a fleet with a timed model (prefill + per-token decode): raise
               L3 or L4 mid-run.  Halt latency, calls started after the raise,
               tokens spent after it, tokens saved vs. running to completion;
               L3 then release: every task finishes exactly once.
  runnode      (not in AxLab: needs the AXIOM loop runner) L4 during a long command -> process tree
               dead.  Kill latency over N trials.
  live         a real model through Ollama (streaming): tokens generated after
               an L4 when in-flight calls DRAIN (what the Python fabric does
               today) vs ABORT (what the extension does: close the stream).

  python experiments/fleet_kill_switch/bench.py all            # ~10 min on a desktop
  python experiments/fleet_kill_switch/bench.py all --quick    # ~1 min smoke run
  python experiments/fleet_kill_switch/bench.py live --model qwen2.5-coder:3b

Nothing here is "measured" until it has run; every JSON records the host.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

KEY_HEX = "00" * 31 + "01"
os.environ.setdefault("NXL_RELEASE_KEY", KEY_HEX)      # never touch ~/.nodexloop
os.environ.setdefault("AXLAB_MASTER_KEY", "bench" + "0" * 59)

import psutil  # noqa: E402

from axlab.governance.fleet_level import FleetSwitch  # noqa: E402

RESULTS = HERE / "results"


def host() -> dict:
    gpu = ""
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {"platform": platform.platform(), "cpu": platform.processor(),
            "cores": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "python": platform.python_version(), "gpu": gpu,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def summary(xs, nd=3):
    if not xs:
        return {}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), nd),
            "sd": round(statistics.stdev(xs), nd) if len(xs) > 1 else 0.0,
            "p50": round(pct(xs, .5), nd), "p95": round(pct(xs, .95), nd),
            "p99": round(pct(xs, .99), nd), "max": round(max(xs), nd), "min": round(min(xs), nd)}


def save(name, data, args):
    RESULTS.mkdir(parents=True, exist_ok=True)
    data = {"experiment": name, "host": host(), "quick": bool(args.quick), **data}
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=1))
    print(f"[{name}] -> {RESULTS / (name + '.json')}")
    return data


def tmpfile():
    return Path(tempfile.mkdtemp(prefix="fks_")) / ".nodexloop" / "governance.json"


# ── 1. overhead ──────────────────────────────────────────────────────────────

def exp_overhead(args):
    n = 50_000 if args.quick else 1_000_000
    reps = 3 if args.quick else 7
    out = {}
    for label, present, poll, scopes in [
        ("cached_absent_1scope", False, 0.05, ()),
        ("cached_present_1scope", True, 0.05, ()),
        ("cached_present_3scopes", True, 0.05, ("loop:a", "run:b")),
        ("uncached_absent", False, 0.0, ()),
        ("uncached_present", True, 0.0, ("loop:a", "run:b")),
    ]:
        f = tmpfile()
        sw = FleetSwitch(f, poll_s=poll)
        if present:
            sw.raise_level("run:b", 1, by="bench")
        sw.level(*scopes)
        k = n if poll else max(1000, n // 50)
        per = []
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            for _ in range(k):
                sw.level(*scopes)
            per.append((time.perf_counter_ns() - t0) / k)
        out[label] = {"calls_per_rep": k, "ns_per_call": summary(per, 1)}
        print(f"  {label:26s} {statistics.median(per):9.1f} ns")
    # Baseline: an empty Python call, to put the number in context.
    def nop(*a):
        return 0
    t0 = time.perf_counter_ns()
    for _ in range(n):
        nop("loop:a", "run:b")
    out["python_noop_call_ns"] = round((time.perf_counter_ns() - t0) / n, 1)
    return save("overhead", {"results": out}, args)


# ── 2. propagation ───────────────────────────────────────────────────────────

READER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from axlab.governance.fleet_level import FleetSwitch
sw = FleetSwitch(sys.argv[2], poll_s=float(sys.argv[3]))
last = -1
print("ready", flush=True)
while True:
    lv = sw.level()
    if lv != last:
        print(lv, repr(time.time()), flush=True)
        last = lv
        if lv == 9: break
    time.sleep(0.0005)      # a fleet checking ~2k times/s
"""


def exp_propagation(args):
    trials = 20 if args.quick else 200
    out = {}
    for poll in (0.01, 0.05, 0.10):
        f = tmpfile()
        w = FleetSwitch(f, poll_s=0.0)
        p = subprocess.Popen([sys.executable, "-c", READER, str(REPO), str(f), str(poll)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"

        def wait_for(level):
            while True:
                line = p.stdout.readline().split()
                if line and int(line[0]) == level:
                    return float(line[1])

        wait_for(0)
        seen, write = [], []
        for _ in range(trials):
            time.sleep(random.uniform(0, poll) + 0.005)     # desync from the reader's poll
            t0 = time.time()
            w.raise_level("fleet", 4, by="bench")
            t1 = time.time()
            ts = wait_for(4)
            seen.append((ts - t0) * 1000)
            write.append((t1 - t0) * 1000)
            w.release("fleet", 0, by="bench", reason="next trial")
            wait_for(0)
        p.kill()
        out[f"poll_{int(poll * 1000)}ms"] = {"raise_to_seen_ms": summary(seen, 2),
                                            "raise_write_ms": summary(write, 2)}
        print(f"  poll {poll*1000:4.0f} ms: seen p50 {pct(seen,.5):6.1f}  p99 {pct(seen,.99):6.1f} ms;"
              f" write p50 {pct(write,.5):5.1f} ms")
    return save("propagation", {"trials": trials, "results": out}, args)


# ── 3. throughput + memory ───────────────────────────────────────────────────

THROUGHPUT_CHILD = r"""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, sys.argv[1])
import psutil
from axlab.fabric.agent_store import AgentMessage, ContinuationStore, EXTERNAL
from axlab.fabric.runtime import SlimFabricRuntime
from axlab.fabric.slim_pool import AgentTemplate, SlimAgentPool
from axlab.fabric.backends import BackendResult
from axlab.governance.fleet_level import FleetSwitch
agents, turns, mode = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
BODY = ("The order shows a delayed shipment; I will confirm the carrier status. " * 16)[:1130]
RESULT = ("record status=open carrier=UPS eta=2026-11-09 " * 10)[:340]
class B:
    name, model = "scripted", "fixed"
    def generate(self, *, system, prompt, max_output_tokens, timeout_s=60.0):
        last = prompt.rfind("[event]")
        text = BODY + ("\nDONE" if prompt.find("[tool ", max(0, last)) != -1 else '\nTOOL lookup {"q": 1}')
        return BackendResult(text=text, input_tokens=(len(system)+len(prompt))//4,
                             output_tokens=len(text)//4, latency_ms=0, backend="s", model="f")
rss = lambda: psutil.Process().memory_info().rss
r0 = rss()
pool = SlimAgentPool(agents)
pool.add_template(AgentTemplate(name="s", role="support", wake_conditions=["order"],
                                system_prompt="You are a support agent.", tool_permissions=["lookup"]).sign())
pool.spawn("s", agents)
d = tempfile.mkdtemp()
store = ContinuationStore(os.path.join(d, "s.db"))
f = os.path.join(d, ".nodexloop", "governance.json")
switch = False if mode == "off" else FleetSwitch(f)
if mode == "on_l1":
    switch.raise_level("fleet", 1, by="bench")
async def lookup(a): return RESULT
rt = SlimFabricRuntime(pool, store, B(), tools={"lookup": lookup}, model_slots=8,
                       max_turns=10**9, switch=switch)
async def main():
    t0 = time.perf_counter()
    for turn in range(turns):
        for i in range(agents):
            store.post(AgentMessage.signed(EXTERNAL, i, f"customer asks about order (turn {turn})"))
            rt.wake(i)
        await rt.run_until_idle()
    return time.perf_counter() - t0
r1 = rss()
wall = asyncio.run(main())
s = rt.stats
print(json.dumps({"mode": mode, "completions": s.steps, "wall_s": wall,
                  "completions_per_s": s.steps / wall, "fleet_rss_mb": (rss() - r1) / 1e6,
                  "base_rss_mb": (r1 - r0) / 1e6, "errors": s.errors}))
"""


def exp_throughput(args):
    agents, turns = (2000, 2) if args.quick else (10_000, 5)
    reps = 2 if args.quick else 5
    rows = []
    order = ["off", "on_absent", "on_l1"]
    for rep in range(reps):
        for mode in (order if rep % 2 == 0 else order[::-1]):   # alternate to cancel drift
            r = subprocess.run([sys.executable, "-c", THROUGHPUT_CHILD, str(REPO), str(agents),
                                str(turns), mode], capture_output=True, text=True, timeout=1800)
            row = json.loads(r.stdout.strip().splitlines()[-1])
            row["rep"] = rep
            rows.append(row)
            print(f"  rep {rep} {mode:10s} {row['completions_per_s']:8.0f}/s  fleet RSS {row['fleet_rss_mb']:6.1f} MB")
    agg = {}
    for mode in order:
        rs = [r for r in rows if r["mode"] == mode]
        agg[mode] = {"completions_per_s": summary([r["completions_per_s"] for r in rs], 1),
                     "fleet_rss_mb": summary([r["fleet_rss_mb"] for r in rs], 1),
                     "completions": rs[0]["completions"], "errors": sum(r["errors"] for r in rs)}
    off = agg["off"]["completions_per_s"]["mean"]
    for mode in ("on_absent", "on_l1"):
        agg[mode]["overhead_pct_vs_off"] = round(100 * (off - agg[mode]["completions_per_s"]["mean"]) / off, 2)
    return save("throughput", {"agents": agents, "turns": turns, "slots": 8, "reps": reps,
                               "rows": rows, "results": agg}, args)


# ── 4. halt under load ───────────────────────────────────────────────────────

def _halt_trial(level, *, agents, slots, prefill_ms, ms_per_token, out_tokens, in_tokens,
                raise_at_s, poll_s, release_after_s=None):
    for mod in list(sys.modules):
        if mod.startswith(("axlab.fabric", "axlab.signing")):
            sys.modules.pop(mod, None)
    from axlab.fabric.agent_store import ContinuationStore
    from axlab.fabric.runtime import SlimFabricRuntime
    from axlab.fabric.slim_pool import AgentTemplate, SlimAgentPool
    from axlab.fabric.backends import BackendResult

    f = tmpfile()
    calls = []          # (start, end, in, out)
    lock = threading.Lock()
    raise_t = [None]

    class Timed:
        name, model = "timed", "sim"

        def generate(self, *, system, prompt, max_output_tokens, timeout_s=60.0):
            t0 = time.perf_counter()
            time.sleep((prefill_ms + ms_per_token * out_tokens) / 1000)
            last = prompt.rfind("[event]")
            text = "DONE" if prompt.find("[tool ", max(0, last)) != -1 else 'TOOL lookup {"q": 1}'
            with lock:
                calls.append((t0, time.perf_counter(), in_tokens, out_tokens))
            return BackendResult(text=text, input_tokens=in_tokens, output_tokens=out_tokens,
                                 latency_ms=0, backend="t", model="sim")

    pool = SlimAgentPool(agents)
    pool.add_template(AgentTemplate(name="s", role="support", wake_conditions=["order"],
                                    system_prompt="x", tool_permissions=["lookup"]).sign())
    pool.spawn("s", agents)

    async def lookup(a):
        await asyncio.sleep(0.05)
        return "ok"

    sw = FleetSwitch(f, poll_s=poll_s)
    rt = SlimFabricRuntime(pool, ContinuationStore(), Timed(), tools={"lookup": lookup},
                           model_slots=slots, switch=sw, max_turns=10**9)

    def fire():
        raise_t[0] = time.perf_counter()
        FleetSwitch(f, poll_s=0).raise_level("fleet", level, by="bench")

    async def go():
        from axlab.fabric.agent_store import EXTERNAL, AgentMessage
        for i in range(agents):          # one task per agent
            rt.store.post(AgentMessage.signed(EXTERNAL, i, "customer asks about order"))
            rt.wake(i)
        t0 = time.perf_counter()
        timer = threading.Timer(raise_at_s, fire) if level else None
        if timer:
            timer.start()
        st = await rt.run_until_idle()
        t_ret = time.perf_counter()
        resumed = None
        if level == 3 and release_after_s is not None:
            FleetSwitch(f, poll_s=0).release("fleet", 0, by="bench", reason="resume")
            sw.refresh()
            st = await rt.run_until_idle()
            resumed = {"tasks_done": pool.count_by_state()["DORMANT"], "halted_after": st.halted}
        return t0, t_ret, st, resumed

    t0, t_ret, st, resumed = asyncio.run(go())
    rt_ = raise_t[0]
    res = {"level": level, "halted": st.halted, "steps": st.steps, "archived": st.archived,
           "total_in": sum(c[2] for c in calls), "total_out": sum(c[3] for c in calls),
           "wall_s": t_ret - t0}
    if rt_ is not None:
        # Window: from the raise until the fleet reported halted.  (For L3 the
        # resume after the release is real work, not leakage.)
        end = t_ret
        after_start = [c for c in calls if rt_ < c[0] < end]
        inflight = [c for c in calls if c[0] <= rt_ < c[1]]
        res.update({
            "halt_latency_ms": (t_ret - rt_) * 1000,
            "calls_started_after_raise": len(after_start),
            "calls_in_flight_at_raise": len(inflight),
            # Tokens the model produced after the stop: the decode tail of
            # in-flight calls plus every call that started after it.
            "out_tokens_after_raise": round(sum(
                min(c[3], max(0.0, (min(c[1], end) - max(rt_, c[0] + prefill_ms / 1000)) * 1000 / ms_per_token))
                for c in calls if c[1] > rt_ and c[0] < end)),
        })
    if resumed is not None:
        res["resumed"] = resumed
        res["steps_total_after_resume"] = st.steps
    return res


def exp_halt(args):
    cfg = dict(agents=400 if args.quick else 1000, slots=8, prefill_ms=20.0, ms_per_token=2.0,
               out_tokens=128, in_tokens=320, poll_s=0.05)
    trials = 3 if args.quick else 10
    base = _halt_trial(0, raise_at_s=0, **cfg)
    print(f"  baseline: {base['steps']} calls, {base['total_out']} out tokens, {base['wall_s']:.1f} s")
    raise_at = base["wall_s"] * 0.25
    out = {"config": cfg | {"raise_at_s": round(raise_at, 3)}, "baseline": base}
    for level in (3, 4):
        rows = []
        # L3 trials resume to completion (the slow part), so fewer of them.
        for t in range(trials if level == 4 else max(3, trials // 2)):
            r = _halt_trial(level, raise_at_s=raise_at * random.uniform(0.8, 1.2),
                            release_after_s=0 if level == 3 else None, **cfg)
            r["out_tokens_saved"] = base["total_out"] - r["total_out"] if level == 4 else None
            rows.append(r)
            print(f"  L{level} trial {t}: halt {r['halt_latency_ms']:6.1f} ms, started-after "
                  f"{r['calls_started_after_raise']}, in-flight {r['calls_in_flight_at_raise']}, "
                  f"tokens after {r['out_tokens_after_raise']}"
                  + (f", resumed steps {r['steps_total_after_resume']}" if level == 3 else ""))
        out[f"L{level}"] = {
            "trials": rows,
            "halt_latency_ms": summary([r["halt_latency_ms"] for r in rows], 1),
            "calls_started_after_raise": summary([r["calls_started_after_raise"] for r in rows], 2),
            "calls_in_flight_at_raise": summary([r["calls_in_flight_at_raise"] for r in rows], 2),
            "out_tokens_after_raise": summary([r["out_tokens_after_raise"] for r in rows], 1),
        }
        if level == 4:
            out["L4"]["out_tokens_saved"] = summary([r["out_tokens_saved"] for r in rows], 1)
            out["L4"]["saved_pct"] = round(100 * statistics.fmean(r["out_tokens_saved"] for r in rows)
                                           / base["total_out"], 1)
        else:
            out["L3"]["resume_exactly_once"] = all(
                r["steps_total_after_resume"] == base["steps"] and r["resumed"]["tasks_done"] == cfg["agents"]
                for r in rows)
    return save("halt", out, args)


# ── 5. run-node kill ─────────────────────────────────────────────────────────

def exp_runnode(args):
    """Needs the AXIOM loop runner (axiom_loops), which is not part of AxLab.

    results/runnode.json is the original run, kept for reference; it is not
    reproducible from this repository yet.
    """
    print("  skipped: needs the AXIOM loop runner, not in AxLab yet "
          "(results/runnode.json is the original run, kept for reference)")
    return None


# ── 6. live model: drain vs abort ────────────────────────────────────────────

def _stream(url, model, prompt, max_tokens, stop_flag, abort):
    """One streamed /api/chat call.  Returns (token timestamps, final counts,
    aborted).  With abort=True the stream is closed as soon as stop_flag()."""
    import requests
    body = {"model": model, "stream": True, "keep_alive": "10m",
            "options": {"temperature": 0.3, "num_predict": max_tokens, "seed": 7},
            "messages": [{"role": "system", "content": "You are a support agent. Answer in detail."},
                         {"role": "user", "content": prompt}]}
    stamps, final, aborted = [], {}, False
    with requests.post(f"{url}/api/chat", json=body, stream=True, timeout=600) as r:
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            if d.get("done"):
                final = {"prompt_eval_count": d.get("prompt_eval_count"),
                         "eval_count": d.get("eval_count")}
                break
            stamps.append(time.perf_counter())
            if abort and stop_flag():
                aborted = True
                break               # closing the response closes the connection
    return stamps, final, aborted


def exp_live(args):
    import requests
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    model = args.model
    max_tokens = 64 if args.quick else 256
    calls = 4 if args.quick else 8
    trials = 1 if args.quick else 3
    prompts = [f"Customer {i} asks why order {1000 + i} is late and what a refund would cost. "
               "Explain the policy step by step." for i in range(calls)]
    # Warm the model so load time is not counted.
    _stream(url, model, "hi", 8, lambda: False, False)

    def idle_ttft():
        t0 = time.perf_counter()
        s, _, _ = _stream(url, model, "Say OK.", 4, lambda: False, False)
        return ((s[0] if s else time.perf_counter()) - t0) * 1000

    base_ttft = [idle_ttft() for _ in range(3)]
    out = {"model": model, "max_tokens": max_tokens, "calls": calls,
           "idle_ttft_ms": summary(base_ttft, 1), "ollama_num_parallel": os.environ.get("OLLAMA_NUM_PARALLEL", "default")}

    # Baseline: every call to completion, sequential (one Ollama slot).
    t0 = time.perf_counter()
    base_tokens, per_call_s = 0, []
    for p in prompts:
        c0 = time.perf_counter()
        _, fin, _ = _stream(url, model, p, max_tokens, lambda: False, False)
        base_tokens += fin.get("eval_count") or 0
        per_call_s.append(time.perf_counter() - c0)
    base_wall = time.perf_counter() - t0
    out["baseline"] = {"out_tokens": base_tokens, "wall_s": round(base_wall, 2),
                       "tok_per_s": round(base_tokens / base_wall, 1)}
    print(f"  baseline: {base_tokens} tokens in {base_wall:.1f} s")

    for mode in ("drain", "abort"):
        rows = []
        for t in range(trials):
            f = tmpfile()
            sw = FleetSwitch(f, poll_s=0.05)
            raise_at = base_wall * random.uniform(0.3, 0.45)
            raised = [None]

            def fire():
                raised[0] = time.perf_counter()
                FleetSwitch(f, poll_s=0).raise_level("fleet", 4, by="bench")
            timer = threading.Timer(raise_at, fire)
            stamps_all, counted, started_after, t_end = [], 0, 0, None
            t0 = time.perf_counter()
            timer.start()
            for p in prompts:
                if sw.level() >= 4:          # the fabric's pre-step check
                    break
                if raised[0] is not None:
                    started_after += 1
                st, fin, ab = _stream(url, model, p, max_tokens, lambda: sw.level() >= 4, mode == "abort")
                stamps_all += st
                counted += (fin.get("eval_count") or len(st)) if not ab else len(st)
            t_end = time.perf_counter()
            timer.cancel()
            rt_ = raised[0]
            after = sum(1 for s in stamps_all if rt_ is not None and s > rt_)
            ttft_after = idle_ttft()          # does the server stop generating once we hang up?
            rows.append({"raise_at_s": round(raise_at, 2), "tokens_total": counted,
                         "tokens_after_raise": after, "calls_started_after_raise": started_after,
                         "halt_latency_ms": round((t_end - rt_) * 1000, 1) if rt_ else None,
                         "saved_tokens": base_tokens - counted,
                         "next_request_ttft_ms": round(ttft_after, 1)})
            print(f"  {mode} trial {t}: {after} tokens after the raise, halt "
                  f"{rows[-1]['halt_latency_ms']} ms, next TTFT {ttft_after:.0f} ms")
            time.sleep(1.0)
        out[mode] = {"trials": rows,
                     "tokens_after_raise": summary([r["tokens_after_raise"] for r in rows], 1),
                     "halt_latency_ms": summary([r["halt_latency_ms"] for r in rows], 1),
                     "saved_tokens": summary([r["saved_tokens"] for r in rows], 1),
                     "next_request_ttft_ms": summary([r["next_request_ttft_ms"] for r in rows], 1)}
    return save("live", out, args)


# ── 0. footprint (what a small device pays) ──────────────────────────────────

FOOTPRINT_CHILD = r"""
import json, sys, time, tracemalloc
sys.path.insert(0, sys.argv[1])
import psutil
rss0 = psutil.Process().memory_info().rss
mods0 = set(sys.modules)
tracemalloc.start()
t0 = time.perf_counter()
import axlab.governance.fleet_level as F
t_import = time.perf_counter() - t0
heap_import = tracemalloc.get_traced_memory()[0]
sw = F.FleetSwitch(sys.argv[2], poll_s=0.05)
sw.level("loop:a", "run:b")
heap_switch = tracemalloc.get_traced_memory()[0] - heap_import
tracemalloc.stop()
new = sorted(m for m in set(sys.modules) - mods0 if not m.startswith("_"))
print(json.dumps({"import_ms": t_import * 1000, "import_heap_kb": heap_import / 1024,
                  "switch_object_heap_bytes": heap_switch,
                  "rss_delta_kb": (psutil.Process().memory_info().rss - rss0) / 1024,
                  "modules_pulled_in": new}))
"""


def exp_footprint(args):
    reps = 3 if args.quick else 10
    f = tmpfile()
    w = FleetSwitch(f, poll_s=0)
    w.raise_level("fleet", 1, by="bench")
    w.raise_level("run:b", 2, by="bench")
    w.release("fleet", 1, by="bench", reason="baseline")
    rows = []
    for _ in range(reps):
        r = subprocess.run([sys.executable, "-c", FOOTPRINT_CHILD, str(REPO), str(f)],
                           capture_output=True, text=True, timeout=60)
        rows.append(json.loads(r.stdout.strip().splitlines()[-1]))
    # Import time without tracemalloc running (it slows imports several-fold).
    clean = []
    for _ in range(reps):
        r = subprocess.run([sys.executable, "-c",
                            "import sys,time; sys.path.insert(0, sys.argv[1]); t=time.perf_counter(); "
                            "import axlab.governance.fleet_level; print((time.perf_counter()-t)*1000)", str(REPO)],
                           capture_output=True, text=True, timeout=60)
        clean.append(float(r.stdout.strip()))
    src = (REPO / "axlab" / "governance" / "fleet_level.py").read_bytes()
    third_party = [m for m in rows[0]["modules_pulled_in"]
                   if m.split(".")[0] not in sys.stdlib_module_names and m.split(".")[0] != "axlab"]
    out = {"import_ms": summary(clean, 2),
           "import_ms_under_tracemalloc": summary([r["import_ms"] for r in rows], 2),
           "import_heap_kb": summary([r["import_heap_kb"] for r in rows], 1),
           "switch_object_heap_bytes": summary([r["switch_object_heap_bytes"] for r in rows], 0),
           "rss_delta_kb": summary([r["rss_delta_kb"] for r in rows], 0),
           "third_party_modules": third_party,
           "module_source_bytes": len(src),
           "governance_file_bytes": f.stat().st_size,
           "receipt_bytes_per_change": round(f.with_name("governance-receipts.jsonl").stat().st_size / 3)}
    print(f"  import {out['import_ms']['p50']} ms, switch object {out['switch_object_heap_bytes']['p50']} B, "
          f"file {out['governance_file_bytes']} B, third-party deps {third_party or 'none'}")
    return save("footprint", out, args)


EXPS = {"footprint": exp_footprint, "overhead": exp_overhead, "propagation": exp_propagation, "throughput": exp_throughput,
        "halt": exp_halt, "runnode": exp_runnode, "live": exp_live}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("which", choices=[*EXPS, "all"])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b"))
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    random.seed(args.seed)
    names = list(EXPS) if args.which == "all" else [args.which]
    for n in names:
        print(f"== {n}")
        gc.collect()
        EXPS[n](args)


if __name__ == "__main__":
    main()
