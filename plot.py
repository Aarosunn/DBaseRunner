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

# Files in the results dir that are not per-trial latency CSVs.
_SKIP = {"correctness.csv"}


def read_results(results_dir):
    """Read all per-trial CSV rows from results_dir (skipping non-trial files)."""
    rows = []
    for path in sorted(Path(results_dir).glob("*.csv")):
        if path.name in _SKIP:
            continue
        with open(path, newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def _percentiles(values):
    """(median, p25, p75) — robust for n>=1."""
    med = statistics.median(values)
    if len(values) >= 2:
        q1, _, q3 = statistics.quantiles(values, n=4)
    else:
        q1 = q3 = med
    return med, q1, q3


def aggregate(rows):
    """Aggregate timed rows (warmup=0) into a nested structure:

        {sweep_type: {backend: [ {param, median_ms, p25, p75, median_bytes}, ... ]}}

    Each backend's list is sorted ascending by param.
    """
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


_AXIS_LABEL = {
    "fanout": "fan-out (tweets authored)",
    "selectivity": "selectivity (% of own tweets matching)",
    "hop_depth": "hop depth",
}


def _plot_metric(agg, sweep, figures_dir, *, metric, ylabel, fname, logy):
    import matplotlib
    matplotlib.use("Agg")  # headless: no display on clarity
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    for backend in sorted(agg[sweep]):
        pts = agg[sweep][backend]
        xs = [p["param"] for p in pts]
        ys = [p[metric] for p in pts]
        if metric == "median_ms":
            yerr = [[p["median_ms"] - p["p25"] for p in pts],
                    [p["p75"] - p["median_ms"] for p in pts]]
            ax.errorbar(xs, ys, yerr=yerr, marker="o", capsize=3, label=backend)
        else:
            ax.plot(xs, ys, marker="o", label=backend)
    if logy:
        ax.set_yscale("log")
    ax.set_xlabel(_AXIS_LABEL.get(sweep, sweep))
    ax.set_ylabel(ylabel)
    ax.set_title(f"{sweep} sweep")
    ax.legend()
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    out = Path(figures_dir) / fname
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def render(agg, figures_dir):
    """Render latency + response_bytes figures for every sweep. Returns paths."""
    Path(figures_dir).mkdir(parents=True, exist_ok=True)
    written = []
    fig_name = {"fanout": "fig5_fanout", "selectivity": "fig6_selectivity",
                "hop_depth": "fig7_hop_depth"}
    for sweep in agg:
        base = fig_name.get(sweep, sweep)
        written.append(_plot_metric(
            agg, sweep, figures_dir, metric="median_ms",
            ylabel="median latency (ms)", fname=f"{base}.png", logy=True))
        written.append(_plot_metric(
            agg, sweep, figures_dir, metric="median_bytes",
            ylabel="median response bytes", fname=f"{base}_bytes.png", logy=False))
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
