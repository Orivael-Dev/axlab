# AxLab public release checklist

The upstream `Orivael-Dev/axiom` repository is private. Don't switch its
visibility. AxLab is built from copied files only (see [PROVENANCE.md](PROVENANCE.md)).

## Done (2026-11-07)

- [x] Create the public `Orivael-Dev/axlab` repository from clean copied files, with no history from the private repo.
- [x] Record the exact AXIOM source commit: `2b82899f` ([PROVENANCE.md](PROVENANCE.md)).
- [x] Export the Slim Fabric benchmark and the audited subset it needs (`axlab/fabric/`, `axlab/signing.py`), with tests.
- [x] Export the fleet kill switch (`axlab/governance/fleet_level.py`) and its switch and fabric tests.
- [x] Audit every exported file for credentials, customer data, deployment addresses and private datasets. None found.
- [x] Select a license: PolyForm Noncommercial 1.0.0, with commercial licensing on request.
- [x] Pin dependencies (`requirements-lock.txt`); declare ranges in `pyproject.toml`.
- [x] Add `pytest` and cross-platform CI (Linux, macOS, Windows × Python 3.11–3.13).
- [x] Default runs make no network calls and no paid model requests (Ollama is opt-in and local).
- [x] Tests pass in a fresh virtualenv with no AXIOM install, no inherited `PYTHONPATH`, and no master key.
- [x] The 1k scripted run completes from the clean environment.

## Still open

- [ ] Re-run 10k and 75k scripted runs on the Jetson from this repo; commit the raw JSON and a host manifest under `experiments/slim_fabric/results/`.
- [ ] Attach raw reference JSON and an experiment manifest for the original 75k Jetson run (only a summary exists today).
- [ ] L3/L4 on the Jetson from this repo: transcripts retained/cleared, inbox accounting, RSS before/after.
- [ ] Add a suspended-while-waiting-on-a-tool L3 test.
- [ ] `runnode`: export the AXIOM loop runner (`axiom_loops`) after its own audit, or keep it out and say so (currently out).
- [ ] Watch CI for the timing-sensitive tests (`test_level_check_is_cheap`, the fabric suspend test). On shared runners, mark them flaky in the open rather than deleting them.
- [ ] Android APK: if you publish it, include the artifact hash, app permissions, build instructions, dependencies, source code and a model-license review. Don't assume the APK supports 75k agents.
- [ ] Tag `v0.1.0` only after someone outside Orivael reproduces a run.

## First public milestone

A reader with only Python 3.11+ can clone, install, and complete a
**1,000-agent scripted run**, including reproducible measurements, with no
model download, API key or paid service. Then they can scale to 10k/75k and
optionally add Ollama. *(Met on Windows; CI checks Linux and macOS.)*
