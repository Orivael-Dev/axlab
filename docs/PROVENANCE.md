# Provenance

Everything under `axlab/`, `experiments/` and `tests/` was exported from
Orivael's private AXIOM repository at one commit, by copying named files.
None of AXIOM's history is in this repository.

**Source commit:** `2b82899ff710ab47b79e8f0854dd91d66026cf83` (AXIOM, 2026-11-07), except
the files marked *(8aa1c5b1)* below, re-exported from AXIOM
`8aa1c5b153b082108138a181e769d4e5956a907b` (2026-11-09).

## File map

| AxLab | AXIOM source | Changes |
|---|---|---|
| `axlab/fabric/slim_pool.py` | `axiom_agent_fabric/slim_pool.py` | imports only |
| `axlab/fabric/embedding_board.py` | `axiom_agent_fabric/embedding_board.py` | none |
| `axlab/fabric/agent_store.py` *(8aa1c5b1)* | `axiom_agent_fabric/agent_store.py` | imports only. New since the first export: `drop()` (frees an archived agent's hot context and inbox; history rows stay) |
| `axlab/fabric/runtime.py` *(8aa1c5b1)* | `axiom_agent_fabric/runtime.py` | imports and module names in the docstring. New since the first export: L4 releases archived agents (`store.drop`), and the opt-in `boundaries=` hooks |
| `axlab/fabric/boundaries.py` *(8aa1c5b1)* | `axiom_agent_fabric/boundaries.py` | imports and module names in the docstring |
| `axlab/fabric/backends.py` | `axiom_event_token/backends.py` | cut down to `BackendResult`, `BackendError`, `SLMBackend` and the Ollama client (`LocalNanoBackend`, renamed `OllamaBackend`; the old name is kept as an alias). Hosted backends (NIM, DeepSeek, SubQ, custom, chained, domain-routed) are left out. The Jetson CPU/GPU split hook is replaced by honoring `OLLAMA_NUM_GPU` only. `requests` is imported only when a call is made. |
| `axlab/governance/fleet_level.py` | `axiom_fleet_level.py` | module names in the docstring and CLI `prog` |
| `axlab/signing.py` | `axiom_signing.py` | reads `AXLAB_MASTER_KEY` (then `AXIOM_MASTER_KEY`) when a key is first derived, not at import; same HMAC-SHA256(salt, master) construction |
| `experiments/slim_fabric/bench.py` | `research/simulation/slim_fabric_bench.py` | imports; sets a fresh random `AXLAB_MASTER_KEY` for the run when none is set |
| `experiments/fleet_kill_switch/bench.py` | `research/fleet_kill_switch/bench.py` | imports; `runnode` replaced by a skip notice (it needs `axiom_loops`, which is not exported); a `--results-dir` option added (default unchanged) |
| `experiments/fleet_kill_switch/figures.py` | `research/fleet_kill_switch/figures.py` | none |
| `experiments/fleet_kill_switch/README.md` | `research/fleet_kill_switch/README.md` | paths and module names |
| `experiments/fleet_kill_switch/note.html` | `research/fleet_kill_switch/index.html` | none (the note as published at research.orivael.dev/fleet-kill-switch/) |
| `experiments/fleet_kill_switch/results/*` | `research/fleet_kill_switch/results/*` | absolute paths in `run.log` made relative; the JSON files are unchanged |
| `experiments/fleet_kill_switch/figures/*.svg` | same | none |
| `tests/test_slim_fabric.py` | `tests/test_slim_fabric.py` | imports, key variable name |
| `tests/test_fabric_boundaries.py` *(8aa1c5b1)* | `tests/test_fabric_boundaries.py` | imports, key variable name |
| `tests/test_fleet_level.py` | `tests/test_fleet_level.py` | the switch and slim-fabric sections only. Left out: the `axiom_loops` runner tests (6) and the check against the NodeXLoop extension's source (1). The L3/L4 fabric tests had a timing race (fixed here; see git log). |

Signing namespaces (`b"axiom-slim-agent-template-v1"` and the rest) are
unchanged, so records signed with the same master key verify in both AXIOM and
AxLab.

## Written for AxLab (not exported)

`experiments/bounded_failure/` and `tests/test_bounded_failure_bench.py` are new
(the benchmark lives only here; AXIOM has the code it measures).

`axlab/mcp/` and `tests/test_mcp.py` are new. They're modelled on the
raise-only `nxl_set_level` tool in NodeXLoop's MCP server, but no code was
copied from it.

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
passed (55 tests at export) in a fresh virtualenv with no AXIOM on `PYTHONPATH` and no
master key set.

The 2026-11-09 re-export (`8aa1c5b1`: `agent_store.py`, `runtime.py`,
`boundaries.py`, `tests/test_fabric_boundaries.py`) was read the same way, and
the suite passed (85 tests) in a fresh virtualenv under the same conditions.
Nothing in `experiments/bounded_failure/results/` holds a path or user name.
