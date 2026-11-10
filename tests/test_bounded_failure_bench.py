"""The Bounded Failure Under Agent Escalation track, run small: the bounded arm
must stay at zero on every metric while the stock arm, on the same workload,
does not.  If the bounded arm ever regresses, this fails."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bench():
    spec = importlib.util.spec_from_file_location(
        "bfae_bench", ROOT / "experiments" / "bounded_failure" / "bench.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bfae_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bounded_arm_contains_every_failure(tmp_path):
    bench = _bench()
    rc = bench.main(["--agents", "100", "--seeds", "1", "--results-dir", str(tmp_path),
                     "--name", "t"])
    doc = json.loads((tmp_path / "t.json").read_text())
    stock, bounded = (next(r for r in doc["runs"] if r["arm"] == a) for a in ("stock", "bounded"))

    assert rc == 0 and json.loads((tmp_path / "t.regressions.json").read_text()) == []
    assert all(not v for v in bounded["metrics"].values()), bounded["metrics"]
    assert bounded["detail"]["healthy_completed_clean"] == bounded["detail"]["healthy"]
    assert bounded["detail"]["receipt_chain_verifies"] is True
    assert bounded["detail"]["executions_without_allow_receipt"] == 0

    # The same workload on the stock runtime is not contained: the comparison
    # is the point of the track.
    assert stock["metrics"]["unauthorized_actions_executed"] > 0
    assert stock["metrics"]["retries_beyond_ceiling"] > 0
    assert stock["metrics"]["propagated_failures"] > 0


def test_a_too_tight_budget_is_reported_not_hidden(tmp_path):
    bench = _bench()
    rc = bench.main(["--agents", "100", "--seeds", "1", "--arms", "bounded",
                     "--budget-x", "0.5", "--results-dir", str(tmp_path), "--name", "t"])
    regs = json.loads((tmp_path / "t.regressions.json").read_text())
    assert rc == 1 and any(r["metric"] == "propagated_failures" for r in regs)
