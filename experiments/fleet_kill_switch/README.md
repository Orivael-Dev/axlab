# Fleet kill switch benchmark

Measures the agent fleet kill switch (`axlab.governance.fleet_level`, the L1–L4 ladder
shared by AXIOM loop runs, the slim agent fabric and the NodeXLoop extension).
It answers six questions: what a check costs, how fast a stop reaches another
process, what the switch costs a fleet, how many tokens are spent after a
stop, how fast a running command dies, and the same token question with a
real model.

| experiment | what it measures |
|---|---|
| `overhead` | ns per `FleetSwitch.level()`, cached vs uncached, file absent vs present, 1 vs 3 scopes |
| `propagation` | raise in one process → seen in another, at 10 / 50 / 100 ms poll; cost of a raise to the writer |
| `throughput` | `SlimFabricRuntime`, N agents × T turns, scripted model: switch off / on with no file / on at L1. Completions/s and fleet RSS, alternating order |
| `halt` | timed model (prefill + per-token decode): L3 or L4 at 25% of the run. Halt latency, calls started after the stop, tokens after it, tokens saved; L3 → release finishes every task exactly once |
| `runnode` | **not runnable in AxLab yet** (needs the AXIOM loop runner; `results/runnode.json` is the original run). Run node: L4 during a 60 s command whose grandchild would write a marker. Kill latency; the marker must not appear |
| `live` | real model through Ollama, streamed: tokens generated after an L4 when in-flight calls drain (the Python fabric today) vs abort (the extension closes the stream) |

## Run

```
pip install -e ".[bench]"
python experiments/fleet_kill_switch/bench.py all --quick      # smoke run, ~1 min
python experiments/fleet_kill_switch/bench.py all              # full run
python experiments/fleet_kill_switch/bench.py live --model qwen2.5-coder:3b
python experiments/fleet_kill_switch/figures.py                # results/*.json -> figures/*.svg
```

`live` needs Ollama on `OLLAMA_URL` (default `http://localhost:11434`) with
the model pulled. Every result file records the host it ran on. The release
key is set to a fixed test key (`NXL_RELEASE_KEY`), so nothing under
`~/.nodexloop` is touched.

## Read the numbers carefully

- `halt` uses a simulated model (sleep for prefill + decode). Its tokens are
  computed from those timings, not counted by a real tokenizer. `live` is the
  experiment with real tokens.
- `live` runs one call at a time against one Ollama slot. The pre-step check
  is the same one the fabric makes, but the call loop is the benchmark's own,
  not `SlimFabricRuntime`.
- "abort" is the extension's behaviour (it holds an `AbortController` per
  model call). The Python fabric does not abort a call that is already on a
  model slot; it drops the reply. `live` measures the cost of that.
- Single machine. Propagation goes through the filesystem, so it measures
  this OS's stat/rename path, not a network.

## In AxLab

The results and figures here are the original run behind the published note
([note.html](note.html), also at research.orivael.dev/fleet-kill-switch/):
one Windows desktop, recorded in each JSON's `host`. Re-running writes over
`results/`, so commit your own run separately or copy the folder first.
