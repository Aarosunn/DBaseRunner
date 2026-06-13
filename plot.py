"""Plot benchmark results from results/ CSVs into figures/.

Reads every per-trial CSV in the results dir, drops warmup rows (warmup=1),
aggregates timed rows to median latency + p25/p75 per (sweep_type, backend,
param_value), and renders one figure per sweep_type with all backends overlaid
(matching the paper's Fig 5/6). Also emits a response_bytes sanity figure per
sweep — a flat bytes-vs-param line is the no-op-sweep failure mode (seed spec §5).

Usage:  python plot.py --results results/ --figures figures/
"""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

_SKIP = {"correctness.csv"}

# Display names, colors, markers matching the paper's figure style.
_BACKEND_STYLE = {
    "jac":        {"label": "Jac GTI+FP",    "color": "#2ca02c", "marker": "o"},
    "postgres":   {"label": "PG hand-tuned",  "color": "#1f77b4", "marker": "s"},
    "sqlalchemy": {"label": "SQLAlchemy",     "color": "#9467bd", "marker": "D"},
    "neo4j":      {"label": "Neo4j",          "color": "#d62728", "marker": "^"},
}
_DEFAULT_STYLE = {"label": None, "color": None, "marker": "o"}

_AXIS_LABEL = {
    "fanout":      "Fan-out (tweets authored)",
    "selectivity": "Selectivity (% of own tweets matching like_count > 10)",
    "hop_depth":   "Hop depth",
}

_TITLE = {
    "fanout":      "Fan-out sweep (single-hop load_own_tweets)",
    "selectivity": "Selectivity sweep at fan-out=1000",
}

_CAPTION = {
    "fanout": (
        "Client-side perf_counter timing via HTTP. 2 timed trials per point "
        "(smoke run). All-warm cache. Single-hop workload."
    ),
    "selectivity": (
        "Client-side perf_counter timing via HTTP. 2 timed trials per point "
        "(smoke run). All-warm cache. Fan-out fixed at 1000."
    ),
}


def read_results(results_dir):
    rows = []
    for path in sorted(Path(results_dir).glob("*.csv")):
        if path.name in _SKIP:
            continue
        with open(path, newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def _percentiles(values):
    med = statistics.median(values)
    if len(values) >= 2:
        q1, _, q3 = statistics.quantiles(values, n=4)
    else:
        q1 = q3 = med
    return med, q1, q3


def aggregate(rows):
    buckets = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: {"lat": [], "bytes": []})))
    for r in rows:
        if str(r.get("warmup")) != "0":
            continue
        sweep = r["sweep_type"]
        backend = r["backend"]
        param = int(r["param_value"])
        cell = buckets[sweep][backend][param]
        cell["lat"].append(float(r["latency_ms"]))
        cell["bytes"].append(int(r["response_bytes"]))

    out = {}
    for sweep, by_backend in buckets.items():
        out[sweep] = {}
        for backend, by_param in by_backend.items():
            points = []
            for param in sorted(by_param):
                lat = by_param[param]["lat"]
                byts = by_param[param]["bytes"]
                med, p25, p75 = _percentiles(lat)
                points.append({
                    "param": param,
                    "median_ms": med,
                    "p25": p25,
                    "p75": p75,
                    "median_bytes": statistics.median(byts),
                })
            out[sweep][backend] = points
    return out


def _plot_latency(agg, sweep, figures_dir, fname):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # draw backends in a consistent order (jac first if present, then pg, sqla, neo4j)
    order = ["jac", "postgres", "sqlalchemy", "neo4j"]
    backends_present = list(agg[sweep].keys())
    ordered = [b for b in order if b in backends_present] + \
              [b for b in backends_present if b not in order]

    for backend in ordered:
        pts = agg[sweep][backend]
        style = _BACKEND_STYLE.get(backend, _DEFAULT_STYLE)
        xs = [p["param"] for p in pts]
        ys = [p["median_ms"] for p in pts]
        yerr_lo = [p["median_ms"] - p["p25"] for p in pts]
        yerr_hi = [p["p75"] - p["median_ms"] for p in pts]
        label = style["label"] or backend
        ax.errorbar(
            xs, ys,
            yerr=[yerr_lo, yerr_hi],
            label=label,
            color=style["color"],
            marker=style["marker"],
            markersize=7,
            linewidth=2,
            capsize=3,
            capthick=1.2,
        )

    ax.set_yscale("log")
    ax.set_xlabel(_AXIS_LABEL.get(sweep, sweep), fontsize=12)
    ax.set_ylabel("median latency (ms, log)", fontsize=12)
    ax.set_title(_TITLE.get(sweep, f"{sweep} sweep"), fontsize=13, fontweight="normal")
    ax.legend(loc="upper left", fontsize=10, framealpha=0.9)
    ax.grid(True, which="both", color="#cccccc", linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)

    caption = _CAPTION.get(sweep, "")
    if caption:
        fig.text(0.5, 0.01, caption, ha="center", va="bottom",
                 fontsize=7.5, style="italic", color="#555555")

    fig.tight_layout(rect=[0, 0.04, 1, 1])
    out = Path(figures_dir) / fname
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def _plot_bytes(agg, sweep, figures_dir, fname):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.5, 4.0))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    order = ["jac", "postgres", "sqlalchemy", "neo4j"]
    backends_present = list(agg[sweep].keys())
    ordered = [b for b in order if b in backends_present] + \
              [b for b in backends_present if b not in order]

    for backend in ordered:
        pts = agg[sweep][backend]
        style = _BACKEND_STYLE.get(backend, _DEFAULT_STYLE)
        xs = [p["param"] for p in pts]
        ys = [p["median_bytes"] for p in pts]
        ax.plot(xs, ys, label=style["label"] or backend,
                color=style["color"], marker=style["marker"],
                markersize=7, linewidth=2)

    ax.set_xlabel(_AXIS_LABEL.get(sweep, sweep), fontsize=12)
    ax.set_ylabel("median response bytes", fontsize=12)
    ax.set_title(f"{_TITLE.get(sweep, sweep)} — payload sanity", fontsize=12)
    ax.legend(loc="upper left", fontsize=10, framealpha=0.9)
    ax.grid(True, which="both", color="#cccccc", linewidth=0.6)
    ax.set_axisbelow(True)

    fig.tight_layout()
    out = Path(figures_dir) / fname
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def render(agg, figures_dir):
    Path(figures_dir).mkdir(parents=True, exist_ok=True)
    written = []
    fig_name = {
        "fanout":      "fig5_fanout",
        "selectivity": "fig6_selectivity",
        "hop_depth":   "fig7_hop_depth",
    }
    for sweep in agg:
        base = fig_name.get(sweep, sweep)
        written.append(_plot_latency(agg, sweep, figures_dir, f"{base}.png"))
        written.append(_plot_bytes(agg, sweep, figures_dir, f"{base}_bytes.png"))
    return written


def main(argv=None):
    p = argparse.ArgumentParser(description="Plot JacDB benchmark results")
    p.add_argument("--results", default="results/", help="Directory of per-trial CSVs")
    p.add_argument("--figures", default="figures/", help="Output directory for PNGs")
    args = p.parse_args(argv)

    rows = read_results(args.results)
    if not rows:
        raise SystemExit(f"no result CSVs found in {args.results}")
    agg = aggregate(rows)
    written = render(agg, args.figures)
    for path in written:
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
