# AxLab public release checklist

## Release gate: prepare a clean extraction

The upstream `Orivael-Dev/axiom` repository is private. Do not simply switch its visibility to public.

- [ ] Create a new `Orivael-Dev/axlab` public repo (or initially private while preparing).
- [ ] Record exact AXIOM source commit to export.
- [ ] Export only necessary files for Slim Fabric: `research/simulation/slim_fabric_bench.py`, audited dependencies under `axiom_agent_fabric/` and `axiom_event_token/`, plus dedicated tests.
- [ ] Audit imports for references to internal control-plane, credentials, business customer data, deployment addresses, or private datasets.
- [ ] Include the actual `axiom_fleet_level.py` and relevant governance tests **only if** you choose to release those implementations and their dependency chains.
- [ ] Check all file histories and git-lfs/assets for secrets and sensitive artifacts. Create the repository from clean copied files, **not** a fork retaining private history.
- [ ] Select license after verifying dependencies and rights (Apache-2.0 or MIT are candidates; not automatically chosen).
- [ ] Pin Python dependencies and optional Ollama model/version; use a lockfile or exact versions.
- [ ] Add `pytest`, linting, and cross-platform CI. Note known platform-dependent flaky suspend test rather than hiding it.
- [ ] Verify default run makes **no network calls or paid model requests**.
- [ ] Test on a clean machine with no local AXIOM installation or inherited `PYTHONPATH`.
- [ ] Re-run 1k and 10k scripted runs, test L3/L4 behavior, and check event accounting.
- [ ] Attach raw reference JSON and an experiment manifest for the original 75k Jetson run.
- [ ] If publishing an Android APK, include artifact hash, app permissions, build instructions, dependencies, source code and model-license review. Do not assume the APK supports 75k agents.
- [ ] Tag `v0.1.0` only after independent reproducibility is confirmed.

## Suggested repo layout when real source is added

```text
axlab/
├── README.md
├── LICENSE
├── pyproject.toml
├── axlab/                      # independently importable public packages
│   ├── fabric/                 # audited slim Fabric subset
│   └── governance/             # audited L3/L4 subset, if approved
├── experiments/
│   ├── fabric_density/         # 1k / 10k / 75k presets
│   ├── fleet_governance/       # suspend, resume, terminate, stale events
│   └── pocket_android/         # Android APK runner source / scripts
├── results/                    # raw JSON + host manifests
├── tests/
├── docs/
└── .github/ISSUE_TEMPLATE/
```

## First public milestone

A reader who has only Python 3.11+ should be able to clone, install, and complete a **1,000-agent scripted run**, including reproducible measurements, without any model download, API key, or paid service. Then they can scale to 10k/75k and optionally add Ollama.
