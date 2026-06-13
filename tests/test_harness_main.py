"""TDD tests for harness.py Phase-4 wiring: setup_fn hook, seed verify/guard,
metadata sidecar, hop_depth guard, parser, and a mocked main() integration.

Spec: docs/specs/harness-fix-spec.md §1, §2, §4, §7.
"""

import json
from unittest.mock import MagicMock

import pytest

import harness
import seed_gen


def _trial(latency=1.0, resp_bytes=10):
    """A timed_fn result dict (timed_call's return shape, fair-timing spec §5)."""
    return {"latency_ms": latency, "response_bytes": resp_bytes,
            "server_total_ms": None, "ms_fetch": None, "ms_build": None}


# ── run_sweep setup_fn hook (§1.5) ────────────────────────────────────────────

class TestSetupFnHook:
    def test_setup_fn_called_once_per_param_point(self):
        writer = MagicMock()
        setup = MagicMock()
        harness.run_sweep("jac", "fanout", [100, 250, 500],
                          MagicMock(return_value=_trial()), writer,
                          warmup_count=1, trials=1, setup_fn=setup,
                          timestamp_fn=lambda: 0.0)
        assert setup.call_count == 3
        assert [c.args[0] for c in setup.call_args_list] == [100, 250, 500]

    def test_setup_fn_runs_before_any_row_for_that_point(self):
        rows = []
        writer = MagicMock()
        writer.writerow.side_effect = rows.append
        seen = []

        def setup(pv):
            seen.append((pv, len(rows)))

        harness.run_sweep("jac", "fanout", [100, 250],
                          MagicMock(return_value=_trial()), writer,
                          warmup_count=2, trials=2, setup_fn=setup,
                          timestamp_fn=lambda: 0.0)
        # point 100 set up at 0 rows; point 250 set up after point 100's 4 rows
        assert seen == [(100, 0), (250, 4)]

    def test_setup_fn_exception_propagates(self):
        writer = MagicMock()

        def boom(pv):
            raise RuntimeError("seed failed")

        with pytest.raises(RuntimeError, match="seed failed"):
            harness.run_sweep("jac", "fanout", [100],
                              MagicMock(return_value=_trial()), writer,
                              setup_fn=boom, timestamp_fn=lambda: 0.0)

    def test_none_setup_fn_keeps_old_behavior(self):
        rows = []
        writer = MagicMock()
        writer.writerow.side_effect = rows.append
        harness.run_sweep("jac", "fanout", [100],
                          MagicMock(return_value=_trial()), writer,
                          warmup_count=2, trials=3, timestamp_fn=lambda: 0.0)
        assert len(rows) == 5


# ── hop_depth guard + sweep config (§4) ───────────────────────────────────────

class TestSweepConfig:
    def test_hop_depth_not_in_default_sweeps(self):
        assert "hop_depth" not in harness.SWEEPS
        assert set(harness.SWEEPS) == {"fanout", "selectivity"}

    def test_hop_depth_reserved_in_phase_4_5(self):
        assert harness.PHASE_4_5_SWEEPS == {"hop_depth": [1, 2, 3]}

    def test_sweep_endpoints_map(self):
        assert harness.SWEEP_ENDPOINTS["fanout"] == "load_own_tweets"
        assert harness.SWEEP_ENDPOINTS["selectivity"] == "load_own_tweets"
        assert harness.SWEEP_ENDPOINTS["hop_depth"] == "load_extended_feed"

    def test_check_sweep_supported_rejects_hop_depth(self):
        with pytest.raises(SystemExit, match="Phase 4.5"):
            harness.check_sweep_supported("hop_depth")

    def test_check_sweep_supported_allows_fanout(self):
        harness.check_sweep_supported("fanout")   # no raise


class TestParser:
    def test_default_sweep_is_fanout_and_selectivity(self):
        args = harness._build_parser().parse_args(["--backend", "jac", "--url", "http://x"])
        assert args.sweep == ["fanout", "selectivity"]

    def test_hop_depth_accepted_by_choices(self):
        # parser must accept it so OUR error fires, not argparse's
        args = harness._build_parser().parse_args(
            ["--backend", "jac", "--url", "http://x", "--sweep", "hop_depth"])
        assert args.sweep == ["hop_depth"]

    def test_new_flags_present(self):
        args = harness._build_parser().parse_args(
            ["--backend", "jac", "--url", "http://x",
             "--run-id", "r9", "--seed-dir", "s/", "--skip-seed", "--reset", "--cold-l1"])
        assert args.run_id == "r9"
        assert args.seed_dir == "s/"
        assert args.skip_seed is True
        assert args.reset is True
        assert args.cold_l1 is True

    def test_user_id_flag_removed(self):
        with pytest.raises(SystemExit):
            harness._build_parser().parse_args(
                ["--backend", "jac", "--url", "http://x", "--user-id", "x"])


# ── load_point_spec ───────────────────────────────────────────────────────────

class TestLoadPointSpec:
    def test_reads_existing_spec(self, tmp_path):
        seed_gen.write_all(str(tmp_path))
        spec = harness.load_point_spec(str(tmp_path), "fanout", 250)
        assert spec["expected_matching"] == 63

    def test_missing_spec_system_exits(self, tmp_path):
        with pytest.raises(SystemExit, match="seed spec not found"):
            harness.load_point_spec(str(tmp_path), "fanout", 999)


# ── verify_seed (§1.4) ────────────────────────────────────────────────────────

def _spec(matching=2):
    return {
        "expected_matching": matching,
        "likes_threshold": 10,
        "expected_matching_keys": [f"t_{i:04d}" for i in range(matching)],
    }


def _backend_returning(tweets):
    b = MagicMock()
    b.load_own_tweets.return_value = {"tweets": tweets}
    return b


def _tweet(key, like_count=14):
    # likes cardinality must equal like_count to satisfy verify_seed's H1 gate.
    return {"content": f"[{key}] body", "like_count": like_count,
            "likes": [f"liker_{i:03d}" for i in range(like_count)],
            "comments": [], "author_username": "u", "created_at": "t"}


class TestVerifySeed:
    def test_passes_on_exact_match(self):
        b = _backend_returning([_tweet("t_0000"), _tweet("t_0001")])
        harness.verify_seed(b, _spec(2))   # no raise

    def test_fails_on_count_mismatch(self):
        b = _backend_returning([_tweet("t_0000")])
        with pytest.raises(SystemExit, match="expected 2"):
            harness.verify_seed(b, _spec(2))

    def test_fails_on_sub_threshold_like_count(self):
        b = _backend_returning([_tweet("t_0000", like_count=14),
                                _tweet("t_0001", like_count=9)])
        with pytest.raises(SystemExit, match="like_count"):
            harness.verify_seed(b, _spec(2))

    def test_fails_on_key_set_mismatch(self):
        b = _backend_returning([_tweet("t_0000"), _tweet("t_9999")])
        with pytest.raises(SystemExit, match="key set"):
            harness.verify_seed(b, _spec(2))

    def test_fails_on_empty_likes_when_like_count_positive(self):
        # jac detached-liker risk (HARNESS_REVIEW H1): the scalar like_count says
        # 14 but the likes array came back empty -> thin payload that the old
        # verify (count + threshold + keys only) waved through.
        b = _backend_returning([
            {"content": "[t_0000] body", "like_count": 14, "likes": [],
             "comments": [], "author_username": "u", "created_at": "t"}])
        with pytest.raises(SystemExit, match="likes"):
            harness.verify_seed(b, _spec(1))

    def test_passes_when_likes_cardinality_matches(self):
        b = _backend_returning([
            {"content": "[t_0000] body", "like_count": 13,
             "likes": [f"liker_{i:03d}" for i in range(13)], "comments": [],
             "author_username": "u", "created_at": "t"}])
        harness.verify_seed(b, _spec(1))   # no raise

    def test_likes_cardinality_caps_at_pool_size(self):
        # Generator guarantee is len(likes) == min(like_count, pool_size); if the
        # liker pool were smaller than like_count, the cap (not like_count) is
        # authoritative. Pool of 5, like_count 14 -> exactly 5 likes is correct.
        spec = {"expected_matching": 1, "likes_threshold": 10,
                "expected_matching_keys": ["t_0000"],
                "likers": [f"l{i}" for i in range(5)]}
        b = _backend_returning([
            {"content": "[t_0000] body", "like_count": 14,
             "likes": [f"l{i}" for i in range(5)], "comments": [],
             "author_username": "u", "created_at": "t"}])
        harness.verify_seed(b, spec)   # no raise: 5 == min(14, pool=5)


class TestGuardNotAlreadySeeded:
    def test_empty_passes(self):
        b = _backend_returning([])
        harness.guard_not_already_seeded(b, "bench_r1_fanout_100")  # no raise

    def test_nonempty_system_exits_naming_user(self):
        b = _backend_returning([_tweet("t_0000")])
        with pytest.raises(SystemExit, match="bench_r1_fanout_100"):
            harness.guard_not_already_seeded(b, "bench_r1_fanout_100")


# ── metadata sidecar (§1.7) ───────────────────────────────────────────────────

class TestMetadata:
    def test_writes_meta_json_with_required_fields(self, tmp_path):
        args = harness._build_parser().parse_args(
            ["--backend", "jac", "--url", "http://x", "--run-id", "r5"])
        harness.write_run_metadata(str(tmp_path), args, started_at="A",
                                   finished_at="B", harness_git_sha="deadbeef",
                                   cold_l1=False)
        meta = json.loads((tmp_path / "jac_meta.json").read_text())
        assert meta["run_id"] == "r5"
        assert meta["backend"] == "jac"
        assert meta["likes_threshold"] == seed_gen.LIKES_THRESHOLD
        assert meta["seed_spec_version"] == seed_gen.SPEC_VERSION
        assert meta["cold_l1"] is False
        assert meta["harness_git_sha"] == "deadbeef"
        assert meta["sweeps"] == ["fanout", "selectivity"]


# ── mocked main() integration: empty payload + setup flow (§1.6, §2, §7) ──────

class FakeBackend:
    """Standalone backend double for main(): no real HTTP. Simulates the
    server-side like_count>K predicate so verify_seed passes against real specs."""
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.session = MagicMock()
        resp = MagicMock()
        resp.content = b"{}"
        resp.raise_for_status.return_value = None
        self.session.post.return_value = resp
        self.session.headers = {}
        self._username = None
        self._data = {}          # username -> matching tweets (per-user isolation)
        self.ensure_calls = []

    def health(self):
        return True

    def ensure_user(self, username, password):
        self._username = username
        self.ensure_calls.append(username)

    def auth(self, username, password):
        self._username = username

    def seed(self, spec):
        # mimic the server predicate: only like_count > threshold are returned later
        self._data[self._username] = [
            t for t in spec["tweets"] if t["like_count"] > spec["likes_threshold"]]

    def load_own_tweets(self):
        return {"tweets": [
            {"content": t["content"], "like_count": t["like_count"],
             "likes": t["likers"], "comments": t["comments"],
             "author_username": self._username, "created_at": t["created_at"]}
            for t in self._data.get(self._username, [])]}

    def reset(self):
        pass

    def clear_cache(self):
        pass


class TestMainIntegration:
    def test_main_seeds_and_posts_empty_payload(self, tmp_path, monkeypatch):
        seed_dir = tmp_path / "seed"
        seed_gen.write_all(str(seed_dir))
        out_dir = tmp_path / "results"
        monkeypatch.setattr("backends.JacBackend", FakeBackend)

        captured = {}
        orig_init = FakeBackend.__init__

        def spy_init(self, url):
            orig_init(self, url)
            captured["backend"] = self
        monkeypatch.setattr(FakeBackend, "__init__", spy_init)

        harness.main([
            "--backend", "jac", "--url", "http://x",
            "--run-id", "rT", "--seed-dir", str(seed_dir), "--out", str(out_dir),
            "--sweep", "fanout", "--warmup", "1", "--trials", "1",
        ])

        be = captured["backend"]
        # one eval user per fanout param point
        assert be.ensure_calls == [
            "bench_rT_fanout_100", "bench_rT_fanout_250", "bench_rT_fanout_500",
            "bench_rT_fanout_750", "bench_rT_fanout_1000"]
        # every timed/warmup POST to load_own_tweets used an EMPTY payload
        load_posts = [c for c in be.session.post.call_args_list
                      if c.args and c.args[0].endswith("/walker/load_own_tweets")]
        assert load_posts, "no load_own_tweets POSTs captured"
        assert all(c.kwargs.get("json") == {} for c in load_posts)
        # csv + metadata sidecar written
        assert (out_dir / "jac.csv").exists()
        assert (out_dir / "jac_meta.json").exists()

    def test_main_hop_depth_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backends.JacBackend", FakeBackend)
        with pytest.raises(SystemExit, match="Phase 4.5"):
            harness.main([
                "--backend", "jac", "--url", "http://x",
                "--sweep", "hop_depth", "--seed-dir", str(tmp_path)])
