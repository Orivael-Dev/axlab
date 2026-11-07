"""Static SVG charts from results/*.json for the research note (no scripts).

    python experiments/fleet_kill_switch/figures.py   ->  figures/*.svg

Colours come from the page's CSS variables (var(--on) etc.), so the charts
follow the note's light/dark theme when inlined.
"""
from __future__ import annotations

import json
from pathlib import Path


def _w(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")


HERE = Path(__file__).resolve().parent
R = HERE / "results"
OUT = HERE / "figures"


def load(name):
    p = R / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def hbars(rows, *, unit, title, w=900, label_w=250, fmt="{:,.1f}", log=False):
    """rows: [(label, value, css colour var, note)]"""
    import math
    bh, gap, top = 26, 12, 34
    h = top + len(rows) * (bh + gap) + 10
    vmax = max(r[1] for r in rows) or 1
    span = w - label_w - 290      # room for the value label right of the longest bar

    def x(v):
        if log:
            lo = math.log10(min(r[1] for r in rows if r[1] > 0) / 2)
            return span * (math.log10(max(v, 1e-9)) - lo) / (math.log10(vmax) - lo)
        return span * v / vmax

    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">',
           f'<text x="0" y="16" class="ct">{esc(title)}</text>']
    for i, (lab, v, col, note) in enumerate(rows):
        y = top + i * (bh + gap)
        out.append(f'<text x="{label_w - 10}" y="{y + bh * .68:.0f}" text-anchor="end" class="cl">{esc(lab)}</text>')
        out.append(f'<rect x="{label_w}" y="{y}" width="{max(2, x(v)):.1f}" height="{bh}" rx="4" fill="var({col})"/>')
        out.append(f'<text x="{label_w + max(2, x(v)) + 8:.1f}" y="{y + bh * .68:.0f}" class="cv">'
                   f'{esc(fmt.format(v))} {esc(unit)}{(" · " + esc(note)) if note else ""}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main():
    OUT.mkdir(exist_ok=True)
    made = []

    o = load("overhead")
    if o:
        r = o["results"]
        rows = [("cached, no file", r["cached_absent_1scope"]["ns_per_call"]["p50"], "--on", ""),
                ("cached, file present", r["cached_present_1scope"]["ns_per_call"]["p50"], "--on", ""),
                ("cached, 3 scopes", r["cached_present_3scopes"]["ns_per_call"]["p50"], "--on", ""),
                ("empty Python call", r["python_noop_call_ns"], "--ink-3", "for scale"),
                ("uncached, no file", r["uncached_absent"]["ns_per_call"]["p50"], "--warn", "stat only"),
                ("uncached, file present", r["uncached_present"]["ns_per_call"]["p50"], "--off", "stat + read + verify")]
        _w(OUT / "overhead.svg", hbars(rows, unit="ns", title="Cost of one level() check (median, log scale)",
                                                fmt="{:,.0f}", log=True))
        made.append("overhead")

    p = load("propagation")
    if p:
        rows = []
        for k, v in p["results"].items():
            ms = k.split("_")[1]
            s = v["raise_to_seen_ms"]
            rows.append((f"poll {ms}: p50", s["p50"], "--on", ""))
            rows.append((f"poll {ms}: p99", s["p99"], "--curve", f"max {s['max']:.0f} ms"))
        _w(OUT / "propagation.svg", hbars(rows, unit="ms", title="Raise in one process → seen in another"))
        made.append("propagation")

    t = load("throughput")
    if t:
        a = t["results"]
        rows = [("switch off", a["off"]["completions_per_s"]["mean"], "--ink-3", f"±{a['off']['completions_per_s']['sd']:.0f}"),
                ("on, no file", a["on_absent"]["completions_per_s"]["mean"], "--on",
                 f"±{a['on_absent']['completions_per_s']['sd']:.0f} · {-a['on_absent']['overhead_pct_vs_off']:+.1f}%"),
                ("on, fleet at L1", a["on_l1"]["completions_per_s"]["mean"], "--on",
                 f"±{a['on_l1']['completions_per_s']['sd']:.0f} · {-a['on_l1']['overhead_pct_vs_off']:+.1f}%")]
        _w(OUT / "throughput.svg", hbars(rows, unit="completions/s", fmt="{:,.0f}",
                                                  title=f"{t['agents']:,} agents × {t['turns']} turns, scripted model (mean of {t['reps']})"))
        made.append("throughput")

    h = load("halt")
    if h:
        b = h["baseline"]["total_out"]
        l4 = h["L4"]
        spent = b - l4["out_tokens_saved"]["mean"]
        rows = [("no stop (runs to completion)", b, "--off", ""),
                ("L4 at 25% of the run", spent, "--on", f"saved {h['L4']['saved_pct']:.0f}%"),
                ("…of which after the stop", l4["out_tokens_after_raise"]["mean"], "--warn", "in-flight decode tails")]
        _w(OUT / "halt_tokens.svg", hbars(rows, unit="output tokens", fmt="{:,.0f}",
                                                   title=f"Output tokens, {h['config']['agents']:,} agents, simulated model"))
        made.append("halt_tokens")

    lv = load("live")
    if lv:
        rows = [("drain (Python fabric today)", lv["drain"]["tokens_after_raise"]["mean"], "--off", ""),
                ("abort the stream (extension)", lv["abort"]["tokens_after_raise"]["mean"], "--on", "")]
        _w(OUT / "live_after.svg", hbars(rows, unit="tokens after the stop", fmt="{:,.1f}",
                                                  title=f"Real model ({lv['model']}): tokens generated after an L4"))
        made.append("live_after")
    print("figures:", ", ".join(made) or "none (no results yet)")


if __name__ == "__main__":
    main()
