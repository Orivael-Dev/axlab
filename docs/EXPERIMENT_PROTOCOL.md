# Benchmark protocol

1. **Record provenance:** source commit SHA, operating system, CPU/SoC, RAM and swap, model hash/tag/quantization, Python/llama.cpp/Ollama version.
2. **Separate phases:** build, park, route, run workload, govern, and cleanup. Do not combine timings across phases.
3. **Measure memory correctly:** report process RSS, Python-traced heap, transcript bytes still reachable, and any spill-to-disk separately. Clear state does not imply returned RSS.
4. **Measure correctness:** submitted events, routed events, tasks started/completed, failed/rejected/dropped events, tool calls, duplicated resumes, stale post-L4 actions.
5. **Measure inference separately:** scripted model vs real local model; prompt tokens computed/reused; generation tokens; median/p95 latency; slots and context.
6. **Governance scenarios:** L3 suspend/release with exactly-once completion; L4 with active model calls, pending tools, and queued inbox messages. Verify late tool responses cannot restart terminated work.
7. **Repeat and report variability:** at least five scripted runs for comparisons. Publish failures, raw JSON, p50/p95/max and configuration.
8. **Don't compare hardware numbers as if matched:** Jetson vs Android vs desktop tests use different architectures and workloads.
9. **Long-running phone runs:** record battery temperature, OS thermal status, CPU frequency if available, and battery drain. Phone benchmarks are not completed by a single fast model response.
10. **Security:** disable external tools and API spend by default. Only use synthetic data in public examples.
