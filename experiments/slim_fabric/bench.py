"""Slim agent fabric benchmark — 75k live agents in an 8 GB budget.

Measures, in order:

  1. build    N agents across T templates (10% tagged), optional embedding
              board; bytes held by columns, board and the whole process.
  2. suspend  half the agents parked WAITING_TOOL with a --context-bytes
              transcript each (simulated tool waits — no task behind them).
  3. route    route() latency over --events random events (p50 / p95 / max).
  4. live     (only with --live N) N events through SlimFabricRuntime with a
              real or fake backend: steps, tool calls, messages, wall time.

Targets: PC first, then Jetson Orin Nano Super (8 GB unified memory — the
model, its KV cache and every agent share it).  On the Jetson keep
--hot-budget-mb ≲ 3500 and run Ollama with OLLAMA_NUM_PARALLEL ≥ --slots.

Examples
--------
  # memory + routing only, no model
  python experiments/slim_fabric/bench.py --agents 75000

  # plus 200 events through a fake model (50 ms/turn) and fake 200 ms tools
  python experiments/slim_fabric/bench.py --live 200 --backend fake

  # plus a real local model through Ollama
  python experiments/slim_fabric/bench.py --live 50 \\
      --backend ollama --model qwen2.5:1.5b --slots 2

  # semantic board instead of hashed (needs `ollama pull all-minilm`)
  python experiments/slim_fabric/bench.py --board ollama

Results go to --out (JSON) and stdout.  Nothing here is "measured" until it
has actually been run on the target device — record the host with it.
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
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Templates, rows and messages signed here live only for this run, so a
# fresh random key is enough when none is set.
import secrets  # noqa: E402
os.environ.setdefault("AXLAB_MASTER_KEY", secrets.token_hex(32))

from axlab.fabric.agent_store import AgentContext, ContinuationStore, PendingTool  # noqa: E402
from axlab.fabric.embedding_board import EmbeddingBoard, OllamaEmbedder  # noqa: E402
from axlab.fabric.runtime import SlimFabricRuntime  # noqa: E402
from axlab.fabric.slim_pool import AgentState, AgentTemplate, SlimAgentPool  # noqa: E402
from axlab.fabric.backends import BackendResult, OllamaBackend  # noqa: E402

DOMAINS = [
    ("billing",   ["refund", "invoice", "charge", "billing", "payment"]),
    ("shipping",  ["delivery", "tracking", "package", "shipping", "courier"]),
    ("returns",   ["return", "exchange", "damaged", "warranty"]),
    ("account",   ["password", "login", "account", "email", "2fa"]),
    ("inventory", ["stock", "sku", "reorder", "warehouse", "inventory"]),
    ("scheduling",["appointment", "booking", "calendar", "reschedule"]),
    ("legal",     ["contract", "clause", "nda", "terms", "liability"]),
    ("it",        ["vpn", "laptop", "printer", "wifi", "ticket"]),
    ("hr",        ["leave", "payroll", "benefits", "onboarding"]),
    ("sales",     ["quote", "pricing", "discount", "lead", "demo"]),
]
TAG_POOL = [f"acct{i:04d}" for i in range(2000)]


def _rss_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1e6, 1)
    except ImportError:
        return None


def build_templates(n_templates: int) -> list[AgentTemplate]:
    out = []
    for i in range(n_templates):
        dom, kws = DOMAINS[i % len(DOMAINS)]
        name = f"{dom}-{i // len(DOMAINS)}"
        out.append(AgentTemplate(
            name=name,
            role=f"{dom} support agent handling {', '.join(kws)}",
            wake_conditions=kws,
            system_prompt=(f"You are a {dom} support agent. Answer briefly. "
                           f"Use the lookup tool for facts you do not have."),
            skills=[dom],
            tool_permissions=["lookup"],
            governance_limits=["no_pii_export"],
        ).sign())
    return out


def random_event(rng: random.Random) -> str:
    dom, kws = rng.choice(DOMAINS)
    words = rng.sample(kws, k=min(2, len(kws)))
    tag = f" for {rng.choice(TAG_POOL)}" if rng.random() < 0.3 else ""
    return f"customer asks about {' and '.join(words)}{tag}"


# ── Backends for the live phase ──────────────────────────────────────────


class FakeBackend:
    """First turn of a task calls the lookup tool, the next turn says DONE."""
    name  = "fake"
    model = "scripted"

    def __init__(self, latency_ms: int) -> None:
        self.latency_ms = latency_ms

    def generate(self, *, system: str, prompt: str, max_output_tokens: int,
                 timeout_s: float = 60.0) -> BackendResult:
        time.sleep(self.latency_ms / 1000)
        last_event = prompt.rfind("[event]")
        if prompt.find("[tool ", max(0, last_event)) != -1:
            text = "Found it, replying to the customer.\nDONE"
        else:
            text = 'Checking.\nTOOL lookup {"q": "order"}'
        return BackendResult(text=text,
                             input_tokens=(len(system) + len(prompt)) // 4,
                             output_tokens=len(text) // 4,
                             latency_ms=self.latency_ms, backend=self.name,
                             model=self.model)


def make_backend(args):
    if args.backend == "fake":
        return FakeBackend(args.fake_latency_ms)
    return OllamaBackend(model=args.model, url=args.ollama_url)


# ── Phases ───────────────────────────────────────────────────────────────


def phase_build(args, rng):
    board = None
    if args.board == "hashed":
        board = EmbeddingBoard(args.templates, dim=args.dim)
    elif args.board == "ollama":
        board = EmbeddingBoard(args.templates, dim=args.dim,
                               embedder=OllamaEmbedder(args.embed_model,
                                                       args.ollama_url))
    pool = SlimAgentPool(args.agents, board=board)
    for t in build_templates(args.templates):
        pool.add_template(t)

    t0 = time.perf_counter()
    per_template = args.agents // args.templates
    for ti, t in enumerate(pool.templates):
        count = per_template if ti < args.templates - 1 else args.agents - len(pool)
        n_tagged = int(count * args.tagged_frac)
        for _ in range(n_tagged):
            pool.spawn(t.name, 1, tags=[rng.choice(TAG_POOL)])
        pool.spawn(t.name, count - n_tagged)
    build_s = time.perf_counter() - t0
    return pool, {
        "agents":          len(pool),
        "templates":       len(pool.templates),
        "build_s":         round(build_s, 2),
        "column_bytes":    pool.column_nbytes(),
        "board_rows":      pool.board_rows,
        "board_bytes":     board.nbytes if board is not None else 0,
        "tagged_agents":   len(pool._tags),
    }


def phase_suspend(args, pool, store, rng):
    n = int(len(pool) * args.suspended_frac)
    filler = ("customer transcript line with order details and prior replies. "
              * (args.context_bytes // 64 + 1))[: args.context_bytes]
    t0 = time.perf_counter()
    for i in rng.sample(range(len(pool)), n):
        store.put(i, AgentContext(text=filler,
                                  pending=PendingTool(f"c{i}", "lookup", {"q": i}),
                                  turns=1))
        pool.set_state(i, AgentState.WAITING_TOOL)
    return {
        "suspended":     n,
        "context_bytes": args.context_bytes,
        "suspend_s":     round(time.perf_counter() - t0, 2),
        "hot_contexts":  store.hot_count,
        "hot_bytes":     store.hot_bytes,
        "parked_to_db":  store.parks,
    }


def phase_route(args, pool, rng):
    events = [random_event(rng) for _ in range(args.events)]
    pool.route(events[0], 4)   # warm-up
    times, hits = [], 0
    for e in events:
        t0 = time.perf_counter()
        r = pool.route(e, 4)
        times.append((time.perf_counter() - t0) * 1000)
        hits += bool(r)
    times.sort()
    return {
        "events":   len(events),
        "routed":   hits,
        "p50_ms":   round(statistics.median(times), 3),
        "p95_ms":   round(times[int(0.95 * (len(times) - 1))], 3),
        "max_ms":   round(times[-1], 3),
    }


async def phase_live(args, pool, store, rng):
    async def lookup(a: dict) -> str:
        await asyncio.sleep(args.tool_latency_ms / 1000)
        return f"record for {a.get('q')}: status=open"

    rt = SlimFabricRuntime(pool, store, make_backend(args),
                           tools={"lookup": lookup},
                           model_slots=args.slots,
                           max_output_tokens=args.max_output_tokens)
    t0 = time.perf_counter()
    peak_waiting = 0
    for _ in range(args.live):
        rt.submit(random_event(rng), k=args.k)
    runner = asyncio.create_task(rt.run_until_idle())
    while not runner.done():
        peak_waiting = max(peak_waiting, rt.waiting_on_tools)
        await asyncio.sleep(0.05)
    stats = await runner
    wall = time.perf_counter() - t0
    steps = max(1, stats.steps)
    tasks = sorted(stats.task_ms)

    def pct(xs, q):
        return round(xs[int(q * (len(xs) - 1))], 1) if xs else None

    tok_in_step  = stats.input_tokens / steps
    tok_out_step = stats.output_tokens / steps
    per_task_in  = stats.input_tokens / max(1, len(tasks))
    per_task_out = stats.output_tokens / max(1, len(tasks))
    return {
        "backend":           f"{rt.backend.name}:{rt.backend.model}",
        "events":            args.live,
        "slots":             args.slots,
        "wall_s":            round(wall, 2),
        "steps":             stats.steps,
        "tool_calls":        stats.tool_calls,
        "tool_refusals":     stats.tool_refusals,
        "messages":          stats.messages,
        "errors":            stats.errors,
        "last_errors":       stats.last_errors,
        "peak_tool_waiting": peak_waiting,
        "input_tokens":      stats.input_tokens,
        "output_tokens":     stats.output_tokens,
        "out_tok_per_s":     round(stats.output_tokens / wall, 1) if wall else 0,
        "model_ms_per_step": round(stats.model_ms / steps, 1),
        "tasks_completed":   len(tasks),
        "task_ms_p50":       pct(tasks, 0.5),
        "task_ms_p95":       pct(tasks, 0.95),
        "task_ms_max":       round(tasks[-1], 1) if tasks else None,
        "steps_per_task":    round(stats.steps / max(1, len(tasks)), 2),
        "in_tok_per_step":   round(tok_in_step, 1),
        "out_tok_per_step":  round(tok_out_step, 1),
        "in_tok_per_task":   round(per_task_in, 1),
        "out_tok_per_task":  round(per_task_out, 1),
        "usd_per_1k_tasks":  round(1000 * (per_task_in * args.price_in
                                           + per_task_out * args.price_out) / 1e6, 4),
        "states_after":      pool.count_by_state(),
    }


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agents", type=int, default=75_000)
    ap.add_argument("--templates", type=int, default=50)
    ap.add_argument("--tagged-frac", type=float, default=0.10)
    ap.add_argument("--board", choices=["hashed", "ollama", "none"], default="hashed")
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--embed-model", default="all-minilm")
    ap.add_argument("--suspended-frac", type=float, default=0.5)
    ap.add_argument("--context-bytes", type=int, default=4096)
    ap.add_argument("--hot-budget-mb", type=int, default=3500)
    ap.add_argument("--db", default=None, help="SQLite path (default: temp file)")
    ap.add_argument("--events", type=int, default=500)
    ap.add_argument("--live", type=int, default=0)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--backend", choices=["fake", "ollama"], default="fake")
    ap.add_argument("--model", default=None, help="Ollama model (default $OLLAMA_MODEL)")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--slots", type=int, default=2)
    ap.add_argument("--max-output-tokens", type=int, default=128)
    ap.add_argument("--fake-latency-ms", type=int, default=50)
    ap.add_argument("--tool-latency-ms", type=int, default=200)
    ap.add_argument("--price-in", type=float, default=0.0,
                    help="USD per 1M input tokens, for usd_per_1k_tasks")
    ap.add_argument("--price-out", type=float, default=0.0,
                    help="USD per 1M output tokens, for usd_per_1k_tasks")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    db = args.db or os.path.join(tempfile.mkdtemp(prefix="slim_fabric_"), "store.db")
    report: dict = {
        "host":   {"platform": platform.platform(), "machine": platform.machine(),
                   "python": platform.python_version()},
        "config": vars(args) | {"db": db},
    }

    gc.collect()
    rss0 = _rss_mb()
    tracemalloc.start()

    pool, report["build"] = phase_build(args, rng)
    store = ContinuationStore(db, hot_budget_bytes=args.hot_budget_mb * 1024 ** 2)
    report["suspend"] = phase_suspend(args, pool, store, rng)

    cur, peak = tracemalloc.get_traced_memory()
    rss1 = _rss_mb()
    report["memory"] = {
        "python_heap_mb":      round(cur / 1e6, 1),
        "python_heap_peak_mb": round(peak / 1e6, 1),
        "rss_start_mb":        rss0,
        "rss_after_mb":        rss1,
        "bytes_per_agent":     round(cur / max(1, len(pool))),
    }
    tracemalloc.stop()   # its bookkeeping slows the timing phases

    report["route"] = phase_route(args, pool, rng)
    if args.live:
        report["live"] = asyncio.run(phase_live(args, pool, store, rng))
        report["memory"]["rss_end_mb"] = _rss_mb()
    store.close()

    print(json.dumps(report, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    return report


if __name__ == "__main__":
    main()
