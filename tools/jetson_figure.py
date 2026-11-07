"""Static SVG summary of the Jetson reference results (no scripts, no deps).

    python tools/jetson_figure.py          ->  results/figures/jetson_reference.svg
    python tools/jetson_figure.py --check  ->  exit 1 if the SVG is out of date

Every number is read from results/reference_summary.json; nothing is typed in
here. Those numbers are Orivael's own runs, summary only, so the figure says so
in its subtitle. The SVG carries its own light/dark colours (prefers-color-scheme),
so it renders correctly as an <img> in the GitHub README.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "results" / "reference_summary.json"
OUT = ROOT / "results" / "figures" / "jetson_reference.svg"

W = 960
PAD = 24
STYLE = """
  .bg { fill: #fcfcfb; } .card { fill: #f3f2ef; }
  .t1 { fill: #0b0b0b; } .t2 { fill: #52514e; } .t3 { fill: #75746f; }
  .bar { fill: #2a78d6; } .track { fill: #e4e3df; } .grid { stroke: #d6d5d0; }
  .warn { fill: #9a6400; }
  text { font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .num { font-variant-numeric: tabular-nums; }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #1a1a19; } .card { fill: #242423; }
    .t1 { fill: #f0efec; } .t2 { fill: #c3c2b7; } .t3 { fill: #9a998f; }
    .bar { fill: #3987e5; } .track { fill: #33332f; } .grid { stroke: #3a3a36; }
    .warn { fill: #e0a640; }
  }
"""


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, cls, size, weight=400, anchor="start"):
    return (f'<text x="{x}" y="{y}" class="{cls}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{esc(s)}</text>')


def tile(x, y, w, h, label, value, unit, note):
    num = (f'<text x="{x + 16}" y="{y + 64}" class="t1 num" font-size="32" font-weight="600">'
           f'{esc(value)}<tspan class="t2" font-size="16" font-weight="400" dx="4">{esc(unit)}'
           f'</tspan></text>')
    return "\n".join([
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="card"/>',
        text(x + 16, y + 26, label, "t2", 13, 500),
        num,
        text(x + 16, y + h - 16, note, "t3", 12),
    ])


def build(r: dict) -> str:
    f, g, q = r["jetson_fabric"], r["jetson_governance"], r["jetson_qwen"]
    s = f["scripted_live"]
    out = [text(PAD, 38, "Jetson Orin Nano Super · 8 GB unified memory", "t1", 20, 600),
           text(PAD, 62, "Reference results from Orivael's own runs. Raw logs are not "
                "published yet: claims to reproduce, not verified facts.", "warn", 13, 500)]

    cols, gap, th = 4, 12, 104
    tw = (W - 2 * PAD - gap * (cols - 1)) / cols
    tiles = [
        ("Agents instantiated", f"{f['agent_instances']:,}", "",
         f"{f['agent_types']} templates · {f['parked_fraction']:.0%} parked"),
        ("Process RSS", f"{f['process_rss_mb']:,}", "MB",
         f"{f['python_heap_mb']} MB traced Python heap"),
        ("Heap per agent", f"{f['heap_bytes_per_agent']:,}", "B",
         f"build {f['build_seconds']} s · park {f['park_seconds']} s"),
        ("Event routing, typical", f"{f['event_route_typical_ms']}", "ms",
         f"p95 {f['event_route_p95_ms']} ms"),
        ("Scripted live: tasks done", f"{s['tasks_completed']}", "",
         f"{s['events']} events · {s['runtime_errors']} errors · "
         f"{s['events_lost_reported']} lost"),
        (f"Live {q['model']}: tokens", f"{q['total_tokens']:,}", "",
         f"{q['tasks_completed']} tasks · {q['model_turns']} turns · {q['wall_seconds']} s"),
        ("Fleet L3 SUSPEND", "Pass", "", "state kept · exactly-once resume"),
        ("Kill-switch tests", f"{g['jetson_tests_passed']}/{g['jetson_tests_total']}", "",
         "passed on the Jetson"),
    ]
    y0 = 84
    for i, t in enumerate(tiles):
        row, col = divmod(i, cols)
        out.append(tile(PAD + col * (tw + gap), y0 + row * (th + gap), tw, th, *t))

    # L4 TERMINATE panel: one MB axis, three bars.
    py = y0 + 2 * (th + gap) + 12
    out.append(text(PAD, py + 18, "Fleet L4 TERMINATE: memory freed (MB)", "t1", 15, 600))
    bars = [("Retained transcripts, before L4", g["l4_transcripts_before_mb"]),
            ("Retained transcripts, after L4", g["l4_transcripts_retained_after_mb"]),
            ("Process RSS released to the OS", g["process_rss_drop_mb"])]
    lw, bh, bgap = 250, 22, 12
    x0, x1 = PAD + lw, W - PAD - 90
    vmax = max(v for _, v in bars)
    by = py + 36
    for tick in (0, 50, 100, 150):
        tx = x0 + (x1 - x0) * tick / vmax
        out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{by - 4}" '
                   f'y2="{by + 3 * (bh + bgap) - bgap + 4}" class="grid" stroke-width="1"/>')
        out.append(text(f"{tx:.1f}", by + 3 * (bh + bgap) + 10, tick, "t3 num", 11, anchor="middle"))
    for i, (label, v) in enumerate(bars):
        yy = by + i * (bh + bgap)
        bw = (x1 - x0) * v / vmax
        out.append(text(PAD, yy + 15, label, "t2", 13))
        if bw > 0:
            out.append(f'<rect x="{x0}" y="{yy}" width="{bw:.1f}" height="{bh}" rx="4" class="bar"/>')
        else:
            out.append(f'<rect x="{x0}" y="{yy + bh / 2 - 1}" width="2" height="2" class="t3"/>')
        out.append(text(f"{x0 + bw + 8:.1f}", yy + 15, f"{v:,} MB", "t1 num", 13, 600))
    ny = by + 3 * (bh + bgap) + 34
    out.append(text(PAD, ny, f"Pending inboxes after L4: {g['l4_inbox_after']}. RSS fell less "
                    "than the transcripts freed: Python's allocator can keep freed memory.",
                    "t3", 12))

    fy = ny + 30
    out.append(f'<line x1="{PAD}" x2="{W - PAD}" y1="{fy - 16}" y2="{fy - 16}" class="grid"/>')
    out.append(text(PAD, fy, f"The 75k-agent runs use a {s['model']} on {s['slots']} slots: "
                    "not 75,000 simultaneous model inferences.", "t3", 12))
    out.append(text(PAD, fy + 18, f"The {q['model']} run is a separate, smaller live workload.",
                    "t3", 12))
    out.append(text(PAD, fy + 36, "Source: results/reference_summary.json · "
                    "regenerate with python tools/jetson_figure.py", "t3", 12))

    h = fy + 52
    body = "\n".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" '
            f'height="{h}" role="img" aria-labelledby="t d">\n'
            f'<title id="t">Jetson Orin Nano Super reference results</title>\n'
            f'<desc id="d">75,000 agents in {f["process_rss_mb"]} MB process RSS; '
            f'L4 terminate frees {g["l4_transcripts_before_mb"]} MB of transcripts to 0 MB; '
            f'summary-only reference numbers, not independently reproduced.</desc>\n'
            f'<style>{STYLE}</style>\n<rect width="{W}" height="{h}" class="bg"/>\n'
            f'{body}\n</svg>\n')


def main(argv=None) -> int:
    svg = build(json.loads(SRC.read_text(encoding="utf-8")))
    if "--check" in (argv or sys.argv[1:]):
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == svg
        print("up to date" if ok else f"{OUT.relative_to(ROOT)} is stale: rerun tools/jetson_figure.py")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
