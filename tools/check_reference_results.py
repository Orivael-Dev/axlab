"""Validate consistency of the curated AxLab reference summary.

This does NOT run AXIOM or reproduce any benchmark. It prevents typos in the
published summary while the independently runnable benchmark is being prepared.
"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
report = json.loads((ROOT / "results/reference_summary.json").read_text())
q = report["jetson_qwen"]
assert q["tokens_input"] + q["tokens_output"] == q["total_tokens"]
j = report["jetson_fabric"]
assert j["agent_instances"] == 75000
assert 0 <= j["scripted_live"]["tasks_completed"]
assert j["scripted_live"]["tasks_completed"] <= j["scripted_live"]["events"] * 4, "Tasks can exceed events due to fan-out, within documented bound"
g = report["jetson_governance"]
assert g["l4_transcripts_retained_after_mb"] == 0
assert g["l4_inbox_after"] == 0
print("Reference summary is internally consistent. This is NOT a model/fabric benchmark.")
