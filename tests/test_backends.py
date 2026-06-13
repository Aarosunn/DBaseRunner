"""TDD tests for backend adapters — control-plane contract (harness-fix-spec §3).

HTTP mocked via an injected fake session. Covers: ensure_user/auth token parsing
and header, normalized load_own_tweets() shape (empty payload, comment author
mapping, like_count fallback, neo4j JSON-string comments), health verb per backend,
seed() body shape, reset(), clear_cache().
"""

import json
from unittest.mock import MagicMock

import pytest


from backends import JacBackend, PostgresBackend, SQLAlchemyBackend, Neo4jBackend
from backends.base import extract_server_timing


def fake_resp(json_body=None, content=b"", status=200, raise_exc=None):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.json.return_value = json_body if json_body is not None else {}
    if raise_exc is not None:
        r.raise_for_status.side_effect = raise_exc
    else:
        r.raise_for_status.return_value = None
    return r


def attach(backend):
    """Replace the adapter's live session with a mock; return the mock."""
    s = MagicMock()
    s.headers = {}
    backend.session = s
    return s


# ── token paths per backend (ensure_user / auth) ─────────────────────────────

class TestAuthTokenPaths:
    def test_jac_reads_nested_data_token(self):
        # jac-cloud login envelope: {"ok": true, "data": {"username","token","root_id"}}
        b = JacBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({"data": {"token": "JWT123"}})
        b.auth("u@x", "pw")
        assert s.headers["Authorization"] == "Bearer JWT123"

    def test_postgres_reads_nested_data_token(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({"data": {"token": "bench_u"}})
        b.auth("bench_u", "pw")
        assert s.headers["Authorization"] == "Bearer bench_u"

    def test_sqlalchemy_reads_nested_data_token(self):
        b = SQLAlchemyBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({"data": {"token": "bench_u"}})
        b.auth("bench_u", "pw")
        assert s.headers["Authorization"] == "Bearer bench_u"

    def test_neo4j_reads_result_token(self):
        b = Neo4jBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({"data": {"result": {"token": "bench_u"}}})
        b.auth("bench_u", "pw")
        assert s.headers["Authorization"] == "Bearer bench_u"


class TestEnsureUser:
    def test_registers_then_logs_in(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.side_effect = [
            fake_resp({"data": {"token": "bench_u"}}),   # register
            fake_resp({"data": {"token": "bench_u"}}),   # login
        ]
        b.ensure_user("bench_u", "pw")
        # two POSTs: register, then login
        assert s.post.call_count == 2
        assert s.post.call_args_list[0].args[0].endswith("/user/register")
        assert s.post.call_args_list[1].args[0].endswith("/user/login")
        assert s.headers["Authorization"] == "Bearer bench_u"

    def test_tolerates_register_conflict_then_logs_in(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        import requests
        s.post.side_effect = [
            fake_resp(status=400, raise_exc=requests.HTTPError("exists")),  # register dup
            fake_resp({"data": {"token": "bench_u"}}),                       # login OK
        ]
        b.ensure_user("bench_u", "pw")   # must NOT raise
        assert s.headers["Authorization"] == "Bearer bench_u"

    def test_stores_username_for_seed(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.side_effect = [fake_resp({"data": {"token": "bench_u"}}),
                              fake_resp({"data": {"token": "bench_u"}})]
        b.ensure_user("bench_u", "pw")
        assert b._username == "bench_u"

    def test_jac_register_uses_username_key(self):
        # Cluster-verified: jac-cloud /user/register wants {username, password},
        # NOT an email field; token is nested at data.token.
        b = JacBackend("http://x")
        s = attach(b)
        s.post.side_effect = [fake_resp({"data": {"token": "JWT"}}),
                              fake_resp({"data": {"token": "JWT"}})]
        b.ensure_user("bench_u", "pw")
        reg_body = s.post.call_args_list[0].kwargs["json"]
        assert reg_body.get("username") == "bench_u"
        assert "email" not in reg_body

    def test_baseline_register_uses_username_key(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.side_effect = [fake_resp({"data": {"token": "bench_u"}}),
                              fake_resp({"data": {"token": "bench_u"}})]
        b.ensure_user("bench_u", "pw")
        reg_body = s.post.call_args_list[0].kwargs["json"]
        assert reg_body.get("username") == "bench_u"


# ── load_own_tweets — empty payload + normalized shape ───────────────────────

class TestLoadOwnTweets:
    def _jac_body(self, tweets):
        return {"status": 200, "reports": [{"tweets": tweets}]}

    def _baseline_body(self, tweets):
        return {"data": {"result": tweets, "reports": [{"tweets": tweets}]}}

    def test_posts_empty_payload_to_load_own_tweets(self):
        b = JacBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp(self._jac_body([]))
        b.load_own_tweets()
        url, = s.post.call_args.args
        assert url.endswith("/walker/load_own_tweets")
        assert s.post.call_args.kwargs["json"] == {}

    def test_jac_normalizes_to_common_shape(self):
        b = JacBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp(self._jac_body([{
            "id": "n:Tweet:abc", "content": "[t_0000] hi",
            "author_username": "bench_u", "created_at": "2026-01-01T00:00:00Z",
            "like_count": 14, "likes": ["liker_001", "liker_002"],
            "comments": [{"username": "liker_003", "content": "nice",
                          "created_at": "2026-01-01T00:00:30Z"}],
        }]))
        out = b.load_own_tweets()
        assert list(out.keys()) == ["tweets"]
        t = out["tweets"][0]
        assert t["content"] == "[t_0000] hi"
        assert t["like_count"] == 14
        assert t["likes"] == ["liker_001", "liker_002"]
        assert t["comments"][0] == {"author": "liker_003", "content": "nice",
                                    "created_at": "2026-01-01T00:00:30Z"}
        assert t["raw_id"] == "n:Tweet:abc"

    def test_postgres_maps_comment_username_to_author(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp(self._baseline_body([{
            "id": 7, "content": "[t_0001] x", "author_username": "bench_u",
            "created_at": "2026-01-01T00:01:00Z", "like_count": 12,
            "likes": ["liker_000"],
            "comments": [{"username": "liker_004", "content": "c", "created_at": "t"}],
        }]))
        out = b.load_own_tweets()
        assert out["tweets"][0]["comments"][0]["author"] == "liker_004"

    def test_like_count_falls_back_to_len_likes(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp(self._baseline_body([{
            "id": 1, "content": "c", "author_username": "u",
            "created_at": "t", "likes": ["a", "b", "c"], "comments": [],
        }]))
        out = b.load_own_tweets()
        assert out["tweets"][0]["like_count"] == 3

    def test_neo4j_parses_json_string_comments(self):
        b = Neo4jBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp(self._baseline_body([{
            "id": "bench_u_t_1", "content": "c", "author_username": "u",
            "created_at": "t", "like_count": 11, "likes": ["a"],
            "comments": [json.dumps({"author": "liker_009", "content": "z",
                                     "created_at": "t2"})],
        }]))
        out = b.load_own_tweets()
        assert out["tweets"][0]["comments"][0]["author"] == "liker_009"


# ── health verb per backend ──────────────────────────────────────────────────

class TestHealth:
    def test_jac_health_is_post_walker(self):
        b = JacBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp(status=200)
        assert b.health() is True
        assert s.post.call_args.args[0].endswith("/walker/health")

    def test_baseline_health_is_get(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.get.return_value = fake_resp(status=200)
        assert b.health() is True
        assert s.get.call_args.args[0].endswith("/health")

    def test_health_false_on_exception(self):
        import requests
        b = Neo4jBackend("http://x")
        s = attach(b)
        s.get.side_effect = requests.RequestException("down")
        assert b.health() is False


# ── seed body shape ──────────────────────────────────────────────────────────

SPEC = {
    "likers": ["liker_000", "liker_001"],
    "tweets": [
        {"key": "t_0000", "content": "[t_0000] hi", "created_at": "2026-01-01T00:00:00Z",
         "like_count": 14, "likers": ["liker_000"], "comments": []},
    ],
}


class TestSeed:
    def test_baseline_seed_includes_author_username(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.side_effect = [fake_resp({"data": {"token": "bench_u"}}),
                              fake_resp({"data": {"token": "bench_u"}})]
        b.ensure_user("bench_u", "pw")
        s.post.reset_mock()
        s.post.side_effect = None
        s.post.return_value = fake_resp({})
        b.seed(SPEC)
        url = s.post.call_args.args[0]
        body = s.post.call_args.kwargs["json"]
        assert url.endswith("/walker/seed_tweets")
        assert body["author_username"] == "bench_u"
        assert body["likers"] == ["liker_000", "liker_001"]
        assert body["tweets"][0]["content"] == "[t_0000] hi"
        assert body["tweets"][0]["like_count"] == 14
        assert "key" not in body["tweets"][0]   # key is embedded in content

    def test_jac_seed_omits_author_username(self):
        b = JacBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({})
        b.seed(SPEC)
        body = s.post.call_args.kwargs["json"]
        assert "author_username" not in body
        assert body["tweets"][0]["like_count"] == 14


# ── reset + clear_cache ──────────────────────────────────────────────────────

class TestResetAndClearCache:
    def test_baseline_reset_posts_clear_data(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({})
        b.reset()
        assert s.post.call_args.args[0].endswith("/walker/clear_data")

    def test_jac_reset_is_noop_no_raise(self):
        b = JacBackend("http://x")
        s = attach(b)
        b.reset()   # must not raise, must not POST clear_data
        for c in s.post.call_args_list:
            assert "clear_data" not in c.args[0]

    def test_jac_clear_cache_posts_walker(self):
        b = JacBackend("http://x")
        s = attach(b)
        s.post.return_value = fake_resp({})
        b.clear_cache()
        assert s.post.call_args.args[0].endswith("/walker/clear_cache")

    def test_baseline_clear_cache_is_noop(self):
        b = PostgresBackend("http://x")
        s = attach(b)
        b.clear_cache()
        s.post.assert_not_called()


# ── server-timing extractor (fair-timing spec §5) ────────────────────────────


class TestExtractServerTiming:
    def test_pulls_block_from_baseline_envelope(self):
        body = {"data": {"reports": [{"tweets": [],
                "server_timing": {"ms_fetch": 1.5, "ms_build": 0.5, "server_total": 2.2}}]}}
        out = extract_server_timing(body)
        assert out == {"server_total_ms": 2.2, "ms_fetch": 1.5, "ms_build": 0.5}

    def test_pulls_block_from_jac_envelope(self):
        # jac-cloud omits the data wrapper: {"reports": [...]}
        body = {"reports": [{"tweets": [],
                "server_timing": {"ms_fetch": 3.0, "ms_build": 1.0, "server_total": 4.5}}]}
        out = extract_server_timing(body)
        assert out == {"server_total_ms": 4.5, "ms_fetch": 3.0, "ms_build": 1.0}

    @pytest.mark.parametrize("body", [
        {},                                                   # empty body
        {"data": {}},                                         # no reports
        {"data": {"reports": []}},                            # empty reports
        {"reports": ["notadict"]},                            # reports[0] not a dict
        {"reports": [{"tweets": []}]},                        # no server_timing
        {"reports": [{"server_timing": {"ms_fetch": 1}}]},    # missing keys
        {"reports": [{"server_timing": {"ms_fetch": "x", "ms_build": 0, "server_total": 1}}]},  # non-numeric
    ])
    def test_returns_none_on_malformed(self, body):
        assert extract_server_timing(body) is None

    def test_coerces_int_and_numeric_string_to_float(self):
        body = {"reports": [{"server_timing":
                {"ms_fetch": 2, "ms_build": "0.0", "server_total": "2.7"}}]}
        out = extract_server_timing(body)
        assert out == {"server_total_ms": 2.7, "ms_fetch": 2.0, "ms_build": 0.0}
        assert all(isinstance(v, float) for v in out.values())
