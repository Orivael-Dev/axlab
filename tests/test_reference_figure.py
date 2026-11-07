"""The Jetson reference figure must match results/reference_summary.json."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_jetson_figure_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "jetson_figure.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
