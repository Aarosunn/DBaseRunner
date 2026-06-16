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
import json
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

# Type-selectivity axis (reconciliation spec §8): X% of the eval user's mixed-type
# neighborhood are tweets (the rest are Channel noise); the predicate is gone.
_TYPE_SEL_AXIS = "Type selectivity (% of neighborhood that are tweets)"

_AXIS_LABEL = {
    "fanout":      "Fan-out (tweets authored)",
    "selectivity": _TYPE_SEL_AXIS,
    "hop_depth":   "Hop depth",
}

_SEL_MODE_TITLE = {
    "selectivity_fixed-target": "Type-selectivity (fixed target, n_tweets=1000)",
    "selectivity_fixed-total":  "Type-selectivity reproduction (fixed total=1000)",
}

_TITLE = {
    "fanout":      "Fan-out sweep (single-hop load_own_tweets)",
    "selectivity": "Type-selectivity sweep",
}

_SEL_MODE_CAPTION = {
    "selectivity_fixed-target": "Target tweets fixed at 1000; channel noise varies. "
                                "Returned set constant — slope is type-discrimination cost.",
    "selectivity_fixed-total":  "Neighborhood fixed at 1000; tweet fraction varies "
                                "(reproduces the old fig6 confound — returned set grows).",
}

_CAPTION_SUFFIX = {
    "fanout":      "Single-hop workload.",
    "selectivity": "Mixed-type neighborhood.",
}

# Confound figure overlays the two modes' jac curves on their shared selectivity %.
_CONFOUND_SHARED_PARAMS = [10, 20, 30, 50, 75]


def _axis_label(sweep_key):
    """X-axis label for a series key (handles mode-namespaced selectivity keys)."""
    if sweep_key.startswith("selectivity"):
        return _TYPE_SEL_AXIS
    return _AXIS_LABEL.get(sweep_key, sweep_key)


def _title(sweep_key):
    if sweep_key in _SEL_MODE_TITLE:
        return _SEL_MODE_TITLE[sweep_key]
    if sweep_key.startswith("selectivity"):
        return _TITLE["selectivity"]
    return _TITLE.get(sweep_key, f"{sweep_key} sweep")


def read_run_params(results_dir):
    """Read run parameters (trials, warmup, cold_l1) from the first
    {backend}_meta.json sidecar in results_dir. Returns None if none exist —
    the caption then degrades to the timing method without a fabricated count."""
    for path in sorted(Path(results_dir).glob("*_meta.json")):
        meta = json.loads(path.read_text())
        return {"trials": meta.get("trials"),
                "warmup": meta.get("warmup"),
                "cold_l1": bool(meta.get("cold_l1"))}
    return None


def _caption(sweep, params):
    """Build a figure caption from the run's actual metadata (H3) — never the
    old hardcoded 'smoke run / 2 trials' literal."""
    base = "Client-side perf_counter timing via HTTP."
    if params and params.get("trials") is not None:
        cache = "cold-L1 (jac diagnostic)" if params.get("cold_l1") else "all-warm"
        base += f" {params['trials']} timed trials per point. {cache} cache."
    if sweep in _SEL_MODE_CAPTION:
        suffix = _SEL_MODE_CAPTION[sweep]
    elif sweep.startswith("selectivity"):
        suffix = _CAPTION_SUFFIX["selectivity"]
    else:
        suffix = _CAPTION_SUFFIX.get(sweep, "")
    return f"{base} {suffix}".strip()


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


def _series_key(row):
    """Bucket key for one row. Selectivity rows are namespaced by their mode so the
    two type-selectivity modes plot as distinct figures (reconciliation spec §8);
    a mode-less selectivity row keeps the back-compat 'selectivity' key."""
    if row.get("sweep_type") == "selectivity":
        mode = row.get("selectivity_mode") or ""
        return f"selectivity_{mode}" if mode else "selectivity"
    return row["sweep_type"]


def aggregate(rows):
    buckets = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: {"lat": [], "bytes": []})))
    for r in rows:
        if str(r.get("warmup")) != "0":
            continue
        sweep = _series_key(r)
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


def _plot_latency(agg, sweep, figures_dir, fname, params=None):
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
    ax.set_xlabel(_axis_label(sweep), fontsize=12)
    ax.set_ylabel("median latency (ms, log)", fontsize=12)
    ax.set_title(_title(sweep), fontsize=13, fontweight="normal")
    ax.legend(loc="upper left", fontsize=10, framealpha=0.9)
    ax.grid(True, which="both", color="#cccccc", linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)

    caption = _caption(sweep, params)
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

    ax.set_xlabel(_axis_label(sweep), fontsize=12)
    ax.set_ylabel("median response bytes", fontsize=12)
    ax.set_title(f"{_title(sweep)} — payload sanity", fontsize=12)
    ax.legend(loc="upper left", fontsize=10, framealpha=0.9)
    ax.grid(True, which="both", color="#cccccc", linewidth=0.6)
    ax.set_axisbelow(True)

    fig.tight_layout()
    out = Path(figures_dir) / fname
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def _plot_confound(agg, figures_dir, fname, params=None):
    """Overlay the two type-selectivity modes' jac (GTI) curves on their shared
    selectivity %: fixed-total rises (returned set grows) vs fixed-target flat
    (returned set constant) -> the old 'selectivity' rise was fanout, not
    selectivity (reconciliation spec §5.3)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ft = agg.get("selectivity_fixed-target", {})
    tot = agg.get("selectivity_fixed-total", {})

    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    def _series(by_backend, backend):
        pts = [p for p in by_backend.get(backend, [])
               if p["param"] in _CONFOUND_SHARED_PARAMS]
        return [p["param"] for p in pts], [p["median_ms"] for p in pts]

    plotted = False
    for backend in ("jac",):  # the confound is a jac-vs-jac story (spec §5.3)
        xs_t, ys_t = _series(tot, backend)
        xs_f, ys_f = _series(ft, backend)
        if xs_t:
            ax.plot(xs_t, ys_t, label="fixed-total (returned set grows)",
                    color="#d62728", marker="^", markersize=7, linewidth=2)
            plotted = True
        if xs_f:
            ax.plot(xs_f, ys_f, label="fixed-target (returned set constant)",
                    color="#2ca02c", marker="o", markersize=7, linewidth=2)
            plotted = True

    ax.set_yscale("log")
    ax.set_xlabel(_TYPE_SEL_AXIS, fontsize=12)
    ax.set_ylabel("jac median latency (ms, log)", fontsize=12)
    ax.set_title("Selectivity/fanout confound: same axis, opposite shapes",
                 fontsize=13)
    if plotted:
        ax.legend(loc="upper left", fontsize=10, framealpha=0.9)
    ax.grid(True, which="both", color="#cccccc", linewidth=0.6)
    ax.set_axisbelow(True)

    caption = ("fixed-total reproduces the published fig6 method (the rise is "
               "returned-set growth); fixed-target holds it constant. Shared points "
               f"{_CONFOUND_SHARED_PARAMS}.")
    fig.text(0.5, 0.01, caption, ha="center", va="bottom", fontsize=7.5,
             style="italic", color="#555555")

    fig.tight_layout(rect=[0, 0.04, 1, 1])
    out = Path(figures_dir) / fname
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def render(agg, figures_dir, params=None):
    Path(figures_dir).mkdir(parents=True, exist_ok=True)
    written = []
    # Roster numbering (figure-roster.md): fig1=DBLOC, fig2=Fanout,
    # fig3=Type-Selectivity, fig4=Latency-vs-DBLOC, fig5=Phase-Breakdown.
    # selectivity is mode-namespaced (reconciliation spec §8): fixed-target is the
    # corrected fig3; fixed-total reproduces the old (confounded) fig6 points.
    fig_name = {
        "fanout":                   "fig2_fanout",
        "selectivity":              "figFP_selectivity_provisional",  # mode-less back-compat
        "selectivity_fixed-target": "fig3_type_selectivity",
        "selectivity_fixed-total":  "fig3_repro",
        "hop_depth":                "fig_multihop",
    }
    for sweep in agg:
        base = fig_name.get(sweep, sweep)
        written.append(_plot_latency(agg, sweep, figures_dir, f"{base}.png", params))
        written.append(_plot_bytes(agg, sweep, figures_dir, f"{base}_bytes.png"))

    # Confound overlay when both type-selectivity modes are present (spec §5.3).
    if "selectivity_fixed-target" in agg and "selectivity_fixed-total" in agg:
        written.append(_plot_confound(agg, figures_dir, "fig3_repro_confound.png", params))
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
    params = read_run_params(args.results)
    written = render(agg, args.figures, params)
    for path in written:
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
