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
        assert "fig2_fanout.png" in names
        assert "fig2_fanout_bytes.png" in names
        for p in written:
            assert p.exists() and p.stat().st_size > 0

    def test_read_results_skips_correctness(self, tmp_path):
        _write_csv(tmp_path / "jac.csv", [_row("jac", "fanout", 100, 1.0, 1, 0)])
        _write_csv(tmp_path / "correctness.csv", [_row("x", "y", 1, 1.0, 1, 0)])
        rows = plot.read_results(str(tmp_path))
        assert all(r["backend"] == "jac" for r in rows)

    def test_renders_on_new_timing_schema(self, tmp_path):
        # plot must tolerate the fair-timing columns (server_total_ms/ms_fetch/
        # ms_build/network_ms) — DictReader ignores extras; latency plots unchanged.
        fields = ["backend", "sweep_type", "param_value", "trial_num",
                  "latency_ms", "server_total_ms", "ms_fetch", "ms_build",
                  "network_ms", "response_bytes", "timestamp", "warmup"]
        with open(tmp_path / "postgres.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for param in (100, 250):
                for v in (1.0, 2.0, 3.0):
                    w.writerow({"backend": "postgres", "sweep_type": "fanout",
                                "param_value": param, "trial_num": 0, "latency_ms": v,
                                "server_total_ms": 2.0, "ms_fetch": 1.5, "ms_build": 0.5,
                                "network_ms": v - 2.0, "response_bytes": 100 * param,
                                "timestamp": 0.0, "warmup": 0})
        agg = plot.aggregate(plot.read_results(str(tmp_path)))
        written = plot.render(agg, str(tmp_path / "figs"))
        assert agg["fanout"]["postgres"]
        for p in written:
            assert p.exists() and p.stat().st_size > 0


def _write_csv_mode(path, rows):
    fields = ["backend", "sweep_type", "selectivity_mode", "param_value",
              "trial_num", "latency_ms", "response_bytes", "timestamp", "warmup"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _row_mode(backend, mode, param, lat, byts, warmup=0):
    return {"backend": backend, "sweep_type": "selectivity", "selectivity_mode": mode,
            "param_value": param, "trial_num": 0, "latency_ms": lat,
            "response_bytes": byts, "timestamp": 0.0, "warmup": warmup}


class TestSelectivityModeBucketing:
    def test_aggregate_buckets_selectivity_by_mode(self):
        rows = [_row_mode("jac", "fixed-target", 50, 2.0, 100),
                _row_mode("jac", "fixed-total", 50, 9.0, 100)]
        agg = plot.aggregate(rows)
        assert "selectivity_fixed-target" in agg
        assert "selectivity_fixed-total" in agg
        assert agg["selectivity_fixed-target"]["jac"][0]["median_ms"] == 2.0
        assert agg["selectivity_fixed-total"]["jac"][0]["median_ms"] == 9.0

    def test_modeless_selectivity_stays_back_compat_key(self):
        rows = [_row("jac", "selectivity", 50, 2.0, 100, 0)]
        agg = plot.aggregate(rows)
        assert "selectivity" in agg

    def test_axis_label_is_type_selectivity(self):
        assert "Type selectivity" in plot._axis_label("selectivity_fixed-target")
        assert "Type selectivity" in plot._axis_label("selectivity_fixed-total")


class TestFig3Render:
    def test_fixed_target_renders_fig3_type_selectivity(self, tmp_path):
        rows = [_row_mode("jac", "fixed-target", p, 2.0, 100 * p)
                for p in (10, 20, 30, 50, 75, 100)]
        _write_csv_mode(tmp_path / "jac.csv", rows)
        agg = plot.aggregate(plot.read_results(str(tmp_path)))
        written = plot.render(agg, str(tmp_path / "figs"))
        names = {p.name for p in written}
        assert "fig3_type_selectivity.png" in names

    def test_fixed_total_renders_fig3_repro(self, tmp_path):
        rows = [_row_mode("jac", "fixed-total", p, float(p), 100 * p)
                for p in (2, 5, 10, 20, 30, 50, 75)]
        _write_csv_mode(tmp_path / "jac.csv", rows)
        agg = plot.aggregate(plot.read_results(str(tmp_path)))
        written = plot.render(agg, str(tmp_path / "figs"))
        names = {p.name for p in written}
        assert "fig3_repro.png" in names

    def test_both_modes_render_confound_overlay(self, tmp_path):
        rows = []
        for p in (10, 20, 30, 50, 75, 100):
            rows.append(_row_mode("jac", "fixed-target", p, 2.0, 100))
        for p in (2, 5, 10, 20, 30, 50, 75):
            rows.append(_row_mode("jac", "fixed-total", p, float(p), 100))
        _write_csv_mode(tmp_path / "jac.csv", rows)
        agg = plot.aggregate(plot.read_results(str(tmp_path)))
        written = plot.render(agg, str(tmp_path / "figs"))
        names = {p.name for p in written}
        assert "fig3_repro_confound.png" in names
        for pth in written:
            assert pth.exists() and pth.stat().st_size > 0


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
