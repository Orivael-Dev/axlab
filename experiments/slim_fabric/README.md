# Slim Agent Fabric benchmark

How many live agents fit in a small memory budget, and what does it cost to
route an event to the right one?

Each agent is a packed, HMAC-signed row (~37 bytes of numpy columns) pointing
at a template that's shared and signed once per kind. A suspended agent is
just a continuation: bounded transcript text plus the tool call it's waiting
on, spilled to SQLite when the hot set is over budget. A few model slots
serve every agent.

Code under test: [`axlab/fabric/`](../../axlab/fabric/).

## Phases

| Phase | What it measures |
|---|---|
| build | N agents over T templates (10% tagged), embedding board; bytes held by columns, board and the process |
| suspend | half the agents parked `WAITING_TOOL` with a `--context-bytes` transcript each (simulated tool waits; no task behind them) |
| route | `route()` latency over `--events` random events: p50 / p95 / max |
| live | only with `--live N`: N events through `SlimFabricRuntime` with a scripted or real model; steps, tool calls, tasks, tokens, wall time |

## Run

```bash
pip install -e ".[bench]"

# memory + routing only, no model
python experiments/slim_fabric/bench.py --agents 75000

# plus 200 events through a scripted model (50 ms/turn) and 200 ms tools
python experiments/slim_fabric/bench.py --agents 75000 --live 200 --backend fake \
    --out results/local/my-run.json

# plus a real local model through Ollama
python experiments/slim_fabric/bench.py --agents 75000 --live 40 \
    --backend ollama --model qwen3:0.6b --slots 2

# semantic board instead of hashed (needs `ollama pull all-minilm`)
python experiments/slim_fabric/bench.py --board ollama
```

On an 8 GB Jetson keep `--hot-budget-mb` at or below 3500, and run Ollama
with `OLLAMA_NUM_PARALLEL` ≥ `--slots`.

## Read the numbers carefully

- The suspended agents in the suspend phase are simulated. They hold
  transcripts but have no tool task behind them, so `states_after` will
  still show them as `WAITING_TOOL` after a live run.
- `--backend fake` is a scripted model: the first turn of a task calls the
  `lookup` tool and the next says `DONE`. It measures the fabric, not a model.
- Tasks can exceed events, because one event wakes up to `--k` agents.
- `python_heap_mb` is Python-traced memory; `rss_*` is the whole process.
  Report both. They're different things.
- The output records the host, but not the model hash or the Ollama version.
  Add those to your report by hand.
