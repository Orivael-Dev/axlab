# AxLab

**Reproduce the research. Challenge the benchmarks. Bring your own hardware.**

AxLab is the public experiment lab for Orivael's AXIOM agent infrastructure. Its first release focuses on three research tracks:

1. **Slim Agent Fabric:** lightweight agent density, parked continuations, routing, and shared model slots.
2. **Fleet Governance:** L3 suspend, exactly-once resume, L4 terminate, and retained-state cleanup.
3. **AXIOM Pocket:** local Android inference, model loading, prefix reuse, sustained performance, and thermal behavior.

> **Repository status: release-preparation scaffold.** This package has experiment specifications, curated benchmark summaries, and report templates. It does **not yet include the private AXIOM implementation** or a runnable reproduction of the 75k-agent experiment. Do not describe it as a reproducible release until the source export and clean-machine verification in `docs/RELEASE_CHECKLIST.md` are complete.

## Benchmarks reported by Orivael (reference, not a cross-hardware leaderboard)

| Experiment | Hardware | Observed | Important boundary |
|---|---|---|---|
| Slim Fabric | Jetson Orin Nano Super | 75,000 instantiated agents, 50 templates, half parked; ~350 MB process RSS; 53 MB Python traced heap | Stand-in model for full-fleet workload; **not** 75k simultaneous model inferences |
| Fleet L4 | Jetson Orin Nano Super | Retained transcripts 163.2 MB to 0 MB; pending inboxes emptied | Process RSS dropped only about 22 MB; Python allocator may reuse freed memory |
| Fleet L3 | Jetson Orin Nano Super | State retained and exactly-once resume checks passed | Include suspended-while-tool-waiting scenarios |
| Live local Qwen | Jetson Orin Nano Super | Qwen3-0.6B, 40 events, 45 tasks, 60 turns, 10,790 tokens, 33 s | Separate smaller active workload |
| Pocket APK inference | Samsung SM-F936U, Android 16 | 147 turns; 0.89 s model load, 41.3 output tok/s, 1.08 s median turn | Mobile inference only; no published 75k-agent phone benchmark |

The reference numbers above were supplied from specific research runs. For auditable reproduction, the public release should include full machine-readable logs and exact code revisions.

## Planned quickstarts (activate after source audit)

The original **private** benchmark harness currently documents:

```bash
# No model calls, measurements of memory and routing
python research/simulation/slim_fabric_bench.py --agents 75000

# 75k-agent pool plus scripted 200-event workload
python research/simulation/slim_fabric_bench.py --agents 75000 --live 200 --backend fake --out results/my-jetson-run.json

# Optional real-model run through a locally running Ollama server
python research/simulation/slim_fabric_bench.py --agents 75000 --live 40 --backend ollama --model qwen3:0.6b --slots 2 --out results/my-qwen-run.json
```

**These commands will not run from this scaffold.** They become available only after the benchmark and its audited dependencies are placed into the public repository. For the last command, confirm the exact Ollama model tag on the target system; the tag shown is an example.

## Design principles

- Real measured data vs simulated model vs projected capacity must be clearly labeled.
- Report host hardware, Python/Android version, model ID and quantization, context, slots, seed, repetitions, peak RSS, task correctness, and stop behavior.
- Never claim 0 external API charges means 0 operating cost.
- Governance that clears logical state should be distinguished from operating-system RSS reclamation.
- Failures, missing measurements, and flaky tests remain visible.

## Contribute a run

When source is published, use the hardware benchmark issue template. The most helpful reports include a raw JSON output, exact commit hash, model/backend version, and any correctness failures.

**Contact:** antonio@orivael.dev  
**Research project:** Orivael / AXIOM

## License

**Not yet selected.** Do not publicly release code without an explicit license decision and a provenance/dependency review. See `docs/RELEASE_CHECKLIST.md`.
