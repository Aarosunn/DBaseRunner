"""TDD tests for seed_gen.py — deterministic single-hop seed generator.

Spec: docs/specs/seed-design-spec.md §2-§5. No HTTP, no files (except write_all).
Covers: normative sweep tables, exact selectivity, like_count bands, liker lists,
content keys, monotonic timestamps, comments, byte-determinism, self-checks.
"""

import json

import pytest

import seed_gen


# ── normative sweep tables (§5) ───────────────────────────────────────────────

class TestExpectedMatching:
    @pytest.mark.parametrize("n_tweets,expected", [
        (100, 25), (250, 63), (500, 125), (750, 188), (1000, 250),
    ])
    def test_fanout_table_at_25pct(self, n_tweets, expected):
        assert seed_gen.expected_matching(n_tweets, 25) == expected

    @pytest.mark.parametrize("pct,expected", [
        (10, 100), (25, 250), (50, 500), (75, 750), (100, 1000),
    ])
    def test_selectivity_table_at_1000_tweets(self, pct, expected):
        assert seed_gen.expected_matching(1000, pct) == expected


class TestSeedFloor:
    def test_lowest_fanout_point_clears_min_target_floor(self):
        # HARNESS_CONTEXT.md §8: the lowest sweep point must yield >= ~20-30
        # target nodes, else the result set is too small to be anything but noise.
        _, _, n_matching = seed_gen.point_dimensions("fanout", 100)
        assert n_matching >= 20, (
            f"lowest fanout point yields only {n_matching} matching tweets")


class TestPoints:
    def test_ten_points_total(self):
        assert len(seed_gen.points()) == 10

    def test_fanout_points_fix_selectivity_25(self):
        n, pct, _ = seed_gen.point_dimensions("fanout", 250)
        assert (n, pct) == (250, 25)

    def test_selectivity_points_fix_n_1000(self):
        n, pct, _ = seed_gen.point_dimensions("selectivity", 25)
        assert (n, pct) == (1000, 25)


# ── generate_point structure (§2, §3) ─────────────────────────────────────────

class TestGeneratePoint:
    def test_n_tweets_matches_param_for_fanout(self):
        spec = seed_gen.generate_point("fanout", 250)
        assert spec["n_tweets"] == 250
        assert len(spec["tweets"]) == 250

    def test_n_tweets_fixed_1000_for_selectivity(self):
        spec = seed_gen.generate_point("selectivity", 50)
        assert spec["n_tweets"] == 1000
        assert len(spec["tweets"]) == 1000

    def test_exact_matching_count(self):
        spec = seed_gen.generate_point("fanout", 250)
        matching = [t for t in spec["tweets"] if t["like_count"] > seed_gen.LIKES_THRESHOLD]
        assert len(matching) == 63
        assert spec["expected_matching"] == 63

    def test_matching_keys_match_actual_matching_tweets(self):
        spec = seed_gen.generate_point("selectivity", 25)
        actual = {t["key"] for t in spec["tweets"]
                  if t["like_count"] > seed_gen.LIKES_THRESHOLD}
        assert set(spec["expected_matching_keys"]) == actual
        assert len(spec["expected_matching_keys"]) == spec["expected_matching"]

    def test_matching_like_count_band(self):
        spec = seed_gen.generate_point("fanout", 500)
        for t in spec["tweets"]:
            if t["key"] in set(spec["expected_matching_keys"]):
                assert 11 <= t["like_count"] <= 20
            else:
                assert 0 <= t["like_count"] <= 10

    def test_likers_length_equals_like_count(self):
        spec = seed_gen.generate_point("fanout", 100)
        for t in spec["tweets"]:
            assert len(t["likers"]) == t["like_count"]

    def test_likers_unique_and_from_pool(self):
        spec = seed_gen.generate_point("fanout", 100)
        pool = set(seed_gen.LIKER_POOL)
        for t in spec["tweets"]:
            assert len(set(t["likers"])) == len(t["likers"])
            assert set(t["likers"]) <= pool

    def test_content_starts_with_key_prefix(self):
        spec = seed_gen.generate_point("fanout", 100)
        for i, t in enumerate(spec["tweets"]):
            assert t["key"] == f"t_{i:04d}"
            assert t["content"].startswith(f"[t_{i:04d}] ")

    def test_created_at_strictly_monotonic(self):
        spec = seed_gen.generate_point("fanout", 250)
        ts = [t["created_at"] for t in spec["tweets"]]
        assert ts == sorted(ts)
        assert len(set(ts)) == len(ts)
        assert ts[0] == "2026-01-01T00:00:00Z"
        assert ts[1] == "2026-01-01T00:01:00Z"

    def test_comments_author_from_pool_and_timestamps(self):
        spec = seed_gen.generate_point("fanout", 250)
        pool = set(seed_gen.LIKER_POOL)
        for t in spec["tweets"]:
            assert 0 <= len(t["comments"]) <= 3
            for j, c in enumerate(t["comments"]):
                assert c["author"] in pool
                assert set(c.keys()) == {"author", "content", "created_at"}

    def test_metadata_fields(self):
        spec = seed_gen.generate_point("fanout", 250)
        assert spec["spec_version"] == seed_gen.SPEC_VERSION
        assert spec["sweep_type"] == "fanout"
        assert spec["param_value"] == 250
        assert spec["likes_threshold"] == seed_gen.LIKES_THRESHOLD
        assert spec["selectivity_pct"] == 25
        assert spec["eval_user_suffix"] == "fanout_250"
        assert spec["likers"] == seed_gen.LIKER_POOL


# ── determinism (§2) ──────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_point_byte_identical(self):
        a = json.dumps(seed_gen.generate_point("selectivity", 75), sort_keys=True, indent=1)
        b = json.dumps(seed_gen.generate_point("selectivity", 75), sort_keys=True, indent=1)
        assert a == b

    def test_different_points_differ(self):
        a = seed_gen.generate_point("fanout", 100)
        b = seed_gen.generate_point("fanout", 250)
        assert a["tweets"][0]["content"] != b["tweets"][0]["content"] or a != b


# ── write_all + manifest (§4) ─────────────────────────────────────────────────

class TestWriteAll:
    def test_writes_ten_point_files_and_manifest(self, tmp_path):
        manifest = seed_gen.write_all(str(tmp_path))
        files = sorted(p.name for p in tmp_path.glob("*.json"))
        assert "manifest.json" in files
        assert "fanout_250.json" in files
        assert "selectivity_100.json" in files
        assert len([f for f in files if f != "manifest.json"]) == 10
        assert manifest["spec_version"] == seed_gen.SPEC_VERSION
        assert manifest["likes_threshold"] == seed_gen.LIKES_THRESHOLD

    def test_point_file_roundtrips(self, tmp_path):
        seed_gen.write_all(str(tmp_path))
        spec = json.loads((tmp_path / "fanout_250.json").read_text())
        assert spec["expected_matching"] == 63

    def test_byte_identical_across_runs(self, tmp_path):
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        seed_gen.write_all(str(d1))
        seed_gen.write_all(str(d2))
        f1 = (d1 / "selectivity_50.json").read_bytes()
        f2 = (d2 / "selectivity_50.json").read_bytes()
        assert f1 == f2
