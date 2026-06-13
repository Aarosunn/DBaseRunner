"""Tests for plot.py — aggregation purity + figure rendering.

aggregate() is pure (no matplotlib); render() exercises the headless Agg path.
"""

import csv

import plot


def _write_csv(path, rows):
    fields = ["backend", "sweep_type", "param_value", "trial_num",
              "latency_ms", "response_bytes", "timestamp", "warmup"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _row(backend, sweep, param, lat, byts, warmup):
    return {"backend": backend, "sweep_type": sweep, "param_value": param,
            "trial_num": 0, "latency_ms": lat, "response_bytes": byts,
            "timestamp": 0.0, "warmup": warmup}


class TestAggregate:
    def test_drops_warmup_rows(self):
        rows = [_row("jac", "fanout", 100, 5.0, 10, 1),   # warmup -> ignored
                _row("jac", "fanout", 100, 9.0, 10, 0)]
        agg = plot.aggregate(rows)
        pts = agg["fanout"]["jac"]
        assert len(pts) == 1
        assert pts[0]["median_ms"] == 9.0

    def test_median_and_percentiles(self):
        rows = [_row("pg", "fanout", 100, v, 10, 0) for v in (1.0, 2.0, 3.0, 4.0)]
        agg = plot.aggregate(rows)
        pt = agg["fanout"]["pg"][0]
        assert pt["median_ms"] == 2.5
        assert pt["p25"] <= pt["median_ms"] <= pt["p75"]

    def test_params_sorted_and_grouped(self):
        rows = [_row("jac", "fanout", 500, 3.0, 1, 0),
                _row("jac", "fanout", 100, 1.0, 1, 0),
                _row("jac", "selectivity", 50, 2.0, 1, 0)]
        agg = plot.aggregate(rows)
        assert [p["param"] for p in agg["fanout"]["jac"]] == [100, 500]
        assert "selectivity" in agg
        assert "jac" in agg["selectivity"]

    def test_median_bytes_tracked(self):
        rows = [_row("neo4j", "selectivity", 25, 1.0, 100, 0),
                _row("neo4j", "selectivity", 25, 1.0, 300, 0)]
        agg = plot.aggregate(rows)
        assert agg["selectivity"]["neo4j"][0]["median_bytes"] == 200


class TestRender:
    def test_writes_latency_and_bytes_figures(self, tmp_path):
        csv_path = tmp_path / "jac.csv"
        rows = []
        for param in (100, 250):
            for v in (1.0, 2.0, 3.0):
                rows.append(_row("jac", "fanout", param, v, 100 * param, 0))
        _write_csv(csv_path, rows)

        agg = plot.aggregate(plot.read_results(str(tmp_path)))
        figs = tmp_path / "figs"
        written = plot.render(agg, str(figs))
        names = {p.name for p in written}
        assert "fig5_fanout.png" in names
        assert "fig5_fanout_bytes.png" in names
        for p in written:
            assert p.exists() and p.stat().st_size > 0

    def test_read_results_skips_correctness(self, tmp_path):
        _write_csv(tmp_path / "jac.csv", [_row("jac", "fanout", 100, 1.0, 1, 0)])
        _write_csv(tmp_path / "correctness.csv", [_row("x", "y", 1, 1.0, 1, 0)])
        rows = plot.read_results(str(tmp_path))
        assert all(r["backend"] == "jac" for r in rows)


class TestCaption:
    """H3: caption must reflect the actual run (from _meta.json), not a
    hardcoded 'smoke run / 2 trials' literal."""

    def test_caption_uses_trial_count_not_smoke(self):
        cap = plot._caption("fanout", {"trials": 30, "cold_l1": False})
        assert "30" in cap
        assert "smoke" not in cap.lower()
        assert "2 timed" not in cap

    def test_caption_distinguishes_warm_and_cold(self):
        warm = plot._caption("fanout", {"trials": 30, "cold_l1": False})
        cold = plot._caption("fanout", {"trials": 30, "cold_l1": True})
        assert "warm" in warm.lower()
        assert "cold" in cold.lower()

    def test_caption_handles_missing_params(self):
        # No meta available -> still a string, no crash, no false trial count.
        cap = plot._caption("fanout", None)
        assert isinstance(cap, str)
        assert "smoke" not in cap.lower()

    def test_read_run_params_from_meta(self, tmp_path):
        (tmp_path / "jac_meta.json").write_text(
            '{"trials": 30, "warmup": 20, "cold_l1": false}')
        params = plot.read_run_params(str(tmp_path))
        assert params["trials"] == 30
        assert params["cold_l1"] is False

    def test_read_run_params_none_when_no_meta(self, tmp_path):
        assert plot.read_run_params(str(tmp_path)) is None
