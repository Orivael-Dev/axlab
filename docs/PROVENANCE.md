# Provenance

Everything under `axlab/`, `experiments/` and `tests/` was exported from
Orivael's private AXIOM repository at one commit, by copying named files.
None of AXIOM's history is in this repository.

**Source commit:** `2b82899ff710ab47b79e8f0854dd91d66026cf83` (AXIOM, 2026-11-07)

## File map

| AxLab | AXIOM source | Changes |
|---|---|---|
| `axlab/fabric/slim_pool.py` | `axiom_agent_fabric/slim_pool.py` | imports only |
| `axlab/fabric/embedding_board.py` | `axiom_agent_fabric/embedding_board.py` | none |
| `axlab/fabric/agent_store.py` | `axiom_agent_fabric/agent_store.py` | imports only |
| `axlab/fabric/runtime.py` | `axiom_agent_fabric/runtime.py` | imports and module names in the docstring |
| `axlab/fabric/backends.py` | `axiom_event_token/backends.py` | cut down to `BackendResult`, `BackendError`, `SLMBackend` and the Ollama client (`LocalNanoBackend`, renamed `OllamaBackend`; the old name is kept as an alias). Hosted backends (NIM, DeepSeek, SubQ, custom, chained, domain-routed) are left out. The Jetson CPU/GPU split hook is replaced by honoring `OLLAMA_NUM_GPU` only. `requests` is imported only when a call is made. |
| `axlab/governance/fleet_level.py` | `axiom_fleet_level.py` | module names in the docstring and CLI `prog` |
| `axlab/signing.py` | `axiom_signing.py` | reads `AXLAB_MASTER_KEY` (then `AXIOM_MASTER_KEY`) when a key is first derived, not at import; same HMAC-SHA256(salt, master) construction |
| `experiments/slim_fabric/bench.py` | `research/simulation/slim_fabric_bench.py` | imports; sets a fresh random `AXLAB_MASTER_KEY` for the run when none is set |
| `experiments/fleet_kill_switch/bench.py` | `research/fleet_kill_switch/bench.py` | imports; `runnode` replaced by a skip notice (it needs `axiom_loops`, which is not exported) |
| `experiments/fleet_kill_switch/figures.py` | `research/fleet_kill_switch/figures.py` | none |
| `experiments/fleet_kill_switch/README.md` | `research/fleet_kill_switch/README.md` | paths and module names |
| `experiments/fleet_kill_switch/note.html` | `research/fleet_kill_switch/index.html` | none (the note as published at research.orivael.dev/fleet-kill-switch/) |
| `experiments/fleet_kill_switch/results/*` | `research/fleet_kill_switch/results/*` | absolute paths in `run.log` made relative; the JSON files are unchanged |
| `experiments/fleet_kill_switch/figures/*.svg` | same | none |
| `tests/test_slim_fabric.py` | `tests/test_slim_fabric.py` | imports, key variable name |
| `tests/test_fleet_level.py` | `tests/test_fleet_level.py` | the switch and slim-fabric sections only. Left out: the `axiom_loops` runner tests (6) and the check against the NodeXLoop extension's source (1). |

Signing namespaces (`b"axiom-slim-agent-template-v1"` and the rest) are
unchanged, so records signed with the same master key verify in both AXIOM and
AxLab.

## What the export does not include

- `axiom_loops` (the graph runner), so `runnode` and its tests are left out.
- The NodeXLoop extension. The governance file format and the
  `.nodexloop/governance.json` location are kept so the two stay compatible.
- The rest of `axiom_event_token` and `axiom_agent_fabric` (capsules,
  coordinator, router, power conditioner, event-token agents). The
  benchmarks don't use them, and their dependency chain reaches most of
  AXIOM.
- The note builder (`build_note.py`), which borrows another note's stylesheet.
- Raw logs for the Jetson and Android reference runs. They are not in the
  AXIOM repository either; only the summary in `results/` exists.

## Audit

Before export, every copied file was read in full and checked for secrets,
keys, tokens, customer data, deployment addresses and private datasets. None
were found. The only network code is the Ollama client and embedder, which
connect only to the URL you pass (default `localhost:11434`). The test suite
passed (55 tests) in a fresh virtualenv with no AXIOM on `PYTHONPATH` and no
master key set.
