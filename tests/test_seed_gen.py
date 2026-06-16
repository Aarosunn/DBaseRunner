"""TDD tests for seed_gen.py — deterministic seed generator, SPEC_VERSION 3.

Spec: docs/specs/2026-06-13-selectivity-type-reconciliation.md §5, §6.
v3 changes vs v2: the like_count>K predicate is gone (load_own_tweets returns ALL
own tweets), so selectivity is no longer a like_count distribution. Selectivity is
now a TYPE-selectivity axis with two modes behind --selectivity-mode:
  - fixed-target: n_tweets=1000 const, n_channels=round(1000*(1-s)/s)
  - fixed-total : total=1000 const, n_tweets=s*1000, n_channels=1000-n_tweets
Each selectivity point carries a `channels` noise array. like_count is still seeded
per-tweet for payload realism + the future FP test (#21) but no longer gates output.
"""

import json

import pytest

import seed_gen


# ── version + constants ───────────────────────────────────────────────────────

class TestVersion:
    def test_spec_version_is_3(self):
        assert seed_gen.SPEC_VERSION == 3

    def test_selectivity_modes(self):
        assert set(seed_gen.SELECTIVITY_MODES) == {"fixed-target", "fixed-total"}

    def test_fixed_target_points(self):
        assert seed_gen.SELECTIVITY_VALUES["fixed-target"] == [10, 20, 30, 50, 75, 100]

    def test_fixed_total_points(self):
        assert seed_gen.SELECTIVITY_VALUES["fixed-total"] == [2, 5, 10, 20, 30, 50, 75]


# ── point_dimensions: (n_tweets, n_channels) per mode (§5) ────────────────────

class TestPointDimensions:
    @pytest.mark.parametrize("n", [100, 250, 500, 750, 1000])
    def test_fanout_no_channels(self, n):
        assert seed_gen.point_dimensions("fanout", n) == (n, 0)

    @pytest.mark.parametrize("pct,n_channels", [
        (10, 9000), (20, 4000), (30, 2333), (50, 1000), (75, 333), (100, 0),
    ])
    def test_fixed_target_holds_1000_tweets(self, pct, n_channels):
        n_tweets, ch = seed_gen.point_dimensions("selectivity", pct, "fixed-target")
        assert n_tweets == 1000
        assert ch == n_channels

    @pytest.mark.parametrize("pct,n_tweets,n_channels", [
        (2, 20, 980), (5, 50, 950), (10, 100, 900), (20, 200, 800),
        (30, 300, 700), (50, 500, 500), (75, 750, 250),
    ])
    def test_fixed_total_holds_1000_neighborhood(self, pct, n_tweets, n_channels):
        # Mirrors littleX/results/reproduced_jac_20260529/jac_gtifp_selectivity_sweep.csv
        nt, ch = seed_gen.point_dimensions("selectivity", pct, "fixed-total")
        assert (nt, ch) == (n_tweets, n_channels)
        assert nt + ch == 1000

    def test_unknown_sweep_raises(self):
        with pytest.raises(ValueError):
            seed_gen.point_dimensions("bogus", 10)


# ── points() enumerates fanout + both modes ───────────────────────────────────

class TestPoints:
    def test_total_point_count(self):
        # 5 fanout + 6 fixed-target + 7 fixed-total = 18
        assert len(seed_gen.points()) == 18

    def test_fanout_points_have_no_mode(self):
        fan = [p for p in seed_gen.points() if p[0] == "fanout"]
        assert len(fan) == 5
        assert all(p[2] is None for p in fan)

    def test_selectivity_points_carry_mode(self):
        sel = [p for p in seed_gen.points() if p[0] == "selectivity"]
        modes = {p[2] for p in sel}
        assert modes == {"fixed-target", "fixed-total"}
        assert len([p for p in sel if p[2] == "fixed-target"]) == 6
        assert len([p for p in sel if p[2] == "fixed-total"]) == 7


# ── generate_point structure (§6.1) ───────────────────────────────────────────

class TestGeneratePoint:
    def test_fanout_shape(self):
        spec = seed_gen.generate_point("fanout", 250)
        assert spec["spec_version"] == 3
        assert spec["sweep_type"] == "fanout"
        assert spec["selectivity_mode"] is None
        assert spec["n_tweets"] == 250
        assert spec["n_channels"] == 0
        assert spec["channels"] == []
        assert len(spec["tweets"]) == 250
        assert spec["selectivity_pct"] == 100   # all tweets are the target, no noise

    def test_fixed_target_shape(self):
        spec = seed_gen.generate_point("selectivity", 10, "fixed-target")
        assert spec["selectivity_mode"] == "fixed-target"
        assert spec["selectivity_pct"] == 10
        assert spec["n_tweets"] == 1000
        assert spec["n_channels"] == 9000
        assert len(spec["tweets"]) == 1000
        assert len(spec["channels"]) == 9000

    def test_fixed_total_shape(self):
        spec = seed_gen.generate_point("selectivity", 2, "fixed-total")
        assert spec["selectivity_mode"] == "fixed-total"
        assert spec["n_tweets"] == 20
        assert spec["n_channels"] == 980
        assert len(spec["tweets"]) == 20
        assert len(spec["channels"]) == 980

    def test_channel_keys_and_names(self):
        spec = seed_gen.generate_point("selectivity", 75, "fixed-total")  # 250 channels
        chans = spec["channels"]
        assert chans[0]["key"] == "ch_00000"
        assert all(set(c.keys()) == {"key", "name"} for c in chans)
        assert len({c["key"] for c in chans}) == len(chans)   # unique keys

    def test_tweet_keys_and_content_prefix(self):
        spec = seed_gen.generate_point("fanout", 100)
        for i, t in enumerate(spec["tweets"]):
            assert t["key"] == f"t_{i:04d}"
            assert t["content"].startswith(f"[t_{i:04d}] ")

    def test_like_count_decoupled_realistic(self):
        # v3: like_count is a realistic spread (0..20), NOT a selectivity-driven band.
        spec = seed_gen.generate_point("selectivity", 50, "fixed-target")
        for t in spec["tweets"]:
            assert 0 <= t["like_count"] <= 20
            assert len(t["likers"]) == t["like_count"]
            assert len(set(t["likers"])) == len(t["likers"])
            assert set(t["likers"]) <= set(seed_gen.LIKER_POOL)

    def test_created_at_strictly_monotonic(self):
        spec = seed_gen.generate_point("fanout", 250)
        ts = [t["created_at"] for t in spec["tweets"]]
        assert ts == sorted(ts)
        assert len(set(ts)) == len(ts)
        assert ts[0] == "2026-01-01T00:00:00Z"

    def test_comments_shape(self):
        spec = seed_gen.generate_point("fanout", 250)
        pool = set(seed_gen.LIKER_POOL)
        for t in spec["tweets"]:
            assert 0 <= len(t["comments"]) <= 3
            for c in t["comments"]:
                assert set(c.keys()) == {"author", "content", "created_at"}
                assert c["author"] in pool

    def test_eval_user_suffix_distinguishes_mode(self):
        ft = seed_gen.generate_point("selectivity", 10, "fixed-target")
        tot = seed_gen.generate_point("selectivity", 10, "fixed-total")
        assert ft["eval_user_suffix"] != tot["eval_user_suffix"]
        assert ft["eval_user_suffix"] == "selectivity_fixed-target_10"
        assert tot["eval_user_suffix"] == "selectivity_fixed-total_10"


# ── determinism (§6.1) ────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_point_byte_identical(self):
        a = json.dumps(seed_gen.generate_point("selectivity", 30, "fixed-target"),
                       sort_keys=True, indent=1)
        b = json.dumps(seed_gen.generate_point("selectivity", 30, "fixed-target"),
                       sort_keys=True, indent=1)
        assert a == b

    def test_mode_changes_rng_stream(self):
        # Same param, different mode -> distinct seed string -> distinct tweet content.
        ft = seed_gen.generate_point("selectivity", 10, "fixed-target")
        tot = seed_gen.generate_point("selectivity", 10, "fixed-total")
        assert ft["rng_seed_string"] != tot["rng_seed_string"]


# ── write_all + manifest (§6.2) ───────────────────────────────────────────────

class TestWriteAll:
    def test_writes_eighteen_point_files_and_manifest(self, tmp_path):
        manifest = seed_gen.write_all(str(tmp_path))
        files = sorted(p.name for p in tmp_path.glob("*.json"))
        assert "manifest.json" in files
        assert len([f for f in files if f != "manifest.json"]) == 18
        assert manifest["spec_version"] == 3

    def test_file_naming_includes_mode(self, tmp_path):
        seed_gen.write_all(str(tmp_path))
        names = {p.name for p in tmp_path.glob("*.json")}
        assert "fanout_250.json" in names
        assert "selectivity_fixed-target_10.json" in names
        assert "selectivity_fixed-total_2.json" in names
        # the old mode-less selectivity file must be gone
        assert "selectivity_10.json" not in names

    def test_manifest_records_channel_counts(self, tmp_path):
        manifest = seed_gen.write_all(str(tmp_path))
        pt = next(p for p in manifest["points"]
                  if p["file"] == "selectivity_fixed-target_10.json")
        assert pt["n_tweets"] == 1000
        assert pt["n_channels"] == 9000
        assert pt["selectivity_mode"] == "fixed-target"

    def test_byte_identical_across_runs(self, tmp_path):
        d1, d2 = tmp_path / "a", tmp_path / "b"
        seed_gen.write_all(str(d1))
        seed_gen.write_all(str(d2))
        f1 = (d1 / "selectivity_fixed-total_30.json").read_bytes()
        f2 = (d2 / "selectivity_fixed-total_30.json").read_bytes()
        assert f1 == f2
