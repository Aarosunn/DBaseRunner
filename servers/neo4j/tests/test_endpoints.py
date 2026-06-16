"""TDD tests for Neo4j endpoints — written RED before any implementation.

Endpoints under test: like_tweet, add_comment, follow_user, import_data.
Each is registered under both /walker/<name> and /function/<name>.

Driver is patched at module level so no live Neo4j is required.
"""

import sys
import os
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---------------------------------------------------------------------------
# Module-level driver mock — patched before src.main is imported so the
# module-level `driver = GraphDatabase.driver(...)` call is intercepted.
# ---------------------------------------------------------------------------

_mock_session = MagicMock()
_mock_driver = MagicMock()
_mock_driver.session.return_value.__enter__ = lambda s: _mock_session
_mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

sys.modules.pop("src.main", None)
with patch("neo4j.GraphDatabase.driver", return_value=_mock_driver):
    from src.main import app  # noqa: E402

from starlette.testclient import TestClient


def _auth(username="testuser"):
    return {"Authorization": f"Bearer {username}"}


def setup_function():
    """Reset mock state before each test."""
    _mock_session.reset_mock()
    _mock_session.run.return_value.single.return_value = None
    _mock_session.run.return_value.data.return_value = []


# ---------------------------------------------------------------------------
# like_tweet
# ---------------------------------------------------------------------------

def test_like_tweet_missing_auth_returns_401():
    with TestClient(app) as client:
        resp = client.post("/walker/like_tweet", json={"tweet_id": "t1"})
    assert resp.status_code == 401


def test_like_tweet_happy_path_returns_response_envelope():
    _mock_session.run.return_value.single.return_value = {
        "likes": ["testuser"],
        "liked": True,
    }
    with TestClient(app) as client:
        resp = client.post(
            "/walker/like_tweet",
            json={"tweet_id": "t1"},
            headers=_auth(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "result" in body["data"]
    result = body["data"]["result"]
    assert "liked" in result
    assert "likes" in result


def test_like_tweet_registered_at_function_prefix():
    _mock_session.run.return_value.single.return_value = {"likes": [], "liked": False}
    with TestClient(app) as client:
        resp = client.post(
            "/function/like_tweet",
            json={"tweet_id": "t1"},
            headers=_auth(),
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------------

def test_add_comment_missing_auth_returns_401():
    with TestClient(app) as client:
        resp = client.post(
            "/walker/add_comment",
            json={"tweet_id": "t1", "content": "hello"},
        )
    assert resp.status_code == 401


def test_add_comment_returns_comment_with_correct_fields():
    _mock_session.run.return_value.single.return_value = MagicMock()  # tweet found
    with TestClient(app) as client:
        resp = client.post(
            "/walker/add_comment",
            json={"tweet_id": "t1", "content": "hello"},
            headers=_auth("alice"),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    result = body["data"]["result"]
    assert "comment" in result
    comment = result["comment"]
    assert comment["username"] == "alice"
    assert comment["content"] == "hello"
    assert "created_at" in comment


def test_add_comment_registered_at_function_prefix():
    _mock_session.run.return_value.single.return_value = MagicMock()
    with TestClient(app) as client:
        resp = client.post(
            "/function/add_comment",
            json={"tweet_id": "t1", "content": "hi"},
            headers=_auth(),
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# follow_user
# ---------------------------------------------------------------------------

def test_follow_user_missing_auth_returns_401():
    with TestClient(app) as client:
        resp = client.post("/walker/follow_user", json={"target_id": "bob"})
    assert resp.status_code == 401


def test_follow_user_returns_success():
    with TestClient(app) as client:
        resp = client.post(
            "/walker/follow_user",
            json={"target_id": "bob"},
            headers=_auth(),
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["result"]["success"] is True


def test_follow_user_registered_at_function_prefix():
    with TestClient(app) as client:
        resp = client.post(
            "/function/follow_user",
            json={"target_id": "bob"},
            headers=_auth(),
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# import_data
# ---------------------------------------------------------------------------

def test_import_data_requires_no_auth():
    _mock_session.run.return_value.data.return_value = []
    dataset = {
        "data": {
            "alice": {
                "email": "alice",
                "tweets": [
                    {"content": "hello", "timestamp": "2026-01-01T00:00:00", "likes": 0}
                ],
                "following": [],
            }
        }
    }
    with TestClient(app) as client:
        resp = client.post("/walker/import_data", json=dataset)
    assert resp.status_code == 200
    assert resp.json()["data"]["result"]["success"] is True


def test_import_data_registered_at_function_prefix():
    _mock_session.run.return_value.data.return_value = []
    with TestClient(app) as client:
        resp = client.post("/function/import_data", json={"data": {}})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# add_comment: comment must be stored as a JSON string, not a Python dict.
# Neo4j cannot store list-of-maps as node properties — each comment must be
# json.dumps()'d before being passed to Cypher.
# ---------------------------------------------------------------------------

def test_add_comment_stores_comment_as_json_string():
    import json
    _mock_session.run.return_value.single.return_value = MagicMock()  # tweet found
    with TestClient(app) as client:
        client.post(
            "/walker/add_comment",
            json={"tweet_id": "t1", "content": "hello"},
            headers=_auth("alice"),
        )
    # Find the run() call that has a 'comment' kwarg
    stored = None
    for call in _mock_session.run.call_args_list:
        _, kwargs = call
        if "comment" in kwargs:
            stored = kwargs["comment"]
            break
    assert stored is not None, "No run() call passed 'comment' kwarg"
    assert isinstance(stored, str), (
        f"comment must be JSON string for Neo4j compat, got {type(stored).__name__}: {stored!r}"
    )
    parsed = json.loads(stored)
    assert parsed["username"] == "alice"
    assert parsed["content"] == "hello"
    assert "created_at" in parsed


# ---------------------------------------------------------------------------
# load_own_tweets: comments returned from Neo4j may be JSON strings
# (written by add_comment). They must be deserialized to dicts before
# being included in the response.
# ---------------------------------------------------------------------------

def test_load_own_tweets_deserializes_json_string_comments():
    import json
    comment_str = json.dumps(
        {"username": "alice", "content": "great post", "created_at": "2024-01-01T00:00:00"}
    )
    _mock_session.run.return_value.data.return_value = [
        {
            "id": "t1",
            "content": "hello",
            "author_username": "testuser",
            "created_at": "2024-01-01T00:00:00",
            "like_count": 11,
            "likes": [],
            "comments": [comment_str],
        }
    ]
    with TestClient(app) as client:
        resp = client.post("/walker/load_own_tweets", headers=_auth())
    assert resp.status_code == 200
    tweets = resp.json()["data"]["result"]
    assert len(tweets) == 1
    comments = tweets[0]["comments"]
    assert len(comments) == 1
    assert isinstance(comments[0], dict), (
        f"comment should be dict after deserialization, got {type(comments[0]).__name__}"
    )
    assert comments[0]["username"] == "alice"
    assert comments[0]["content"] == "great post"


# ---------------------------------------------------------------------------
# load_own_tweets reports the server_timing block (fair-timing spec §3):
# ms_fetch = cypher run + .data(); ms_build = list-comprehension build.
# ---------------------------------------------------------------------------

def test_load_own_tweets_reports_server_timing():
    _mock_session.run.return_value.data.return_value = [
        {"id": "t1", "content": "hi", "author_username": "testuser",
         "created_at": "2024-01-01T00:00:00", "like_count": 11, "likes": [], "comments": []}
    ]
    with TestClient(app) as client:
        resp = client.post("/walker/load_own_tweets", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()["data"]
    report = data["reports"][0]
    st = report["server_timing"]
    assert isinstance(st["ms_fetch"], float) and st["ms_fetch"] >= 0
    assert isinstance(st["ms_build"], float) and st["ms_build"] >= 0
    assert isinstance(st["server_total"], float)
    assert st["server_total"] + 1e-6 >= st["ms_fetch"] + st["ms_build"]  # invariant
    # envelope dedup (ledger #3): tweets live ONCE, in data.result — not in the report.
    assert "tweets" not in report
    assert data["result"] == [
        {"id": "t1", "content": "hi", "author_username": "testuser",
         "created_at": "2024-01-01T00:00:00", "like_count": 11, "likes": [], "comments": []}
    ]


# ---------------------------------------------------------------------------
# Type-selectivity reconciliation (spec §4, §10): the like_count>10 predicate is
# DROPPED from load_own_tweets (the :POST edge-type pre-separates the channel
# noise); `threshold` is the filter-pushdown seam (default off).
# ---------------------------------------------------------------------------

def _load_cypher():
    for c in _mock_session.run.call_args_list:
        if c.args and "[:POST]->(t:Tweet)" in c.args[0]:
            return c
    raise AssertionError("no load_own_tweets cypher executed")


def test_load_own_tweets_cypher_has_no_predicate():
    with TestClient(app) as client:
        client.post("/walker/load_own_tweets", headers=_auth())
    cypher = _load_cypher().args[0]
    assert "like_count > 10" not in cypher
    assert "WHERE t.like_count" not in cypher


def test_load_own_tweets_threshold_param_adds_predicate():
    with TestClient(app) as client:
        client.post("/walker/load_own_tweets", headers=_auth(), json={"threshold": 10})
    call = _load_cypher()
    assert "like_count >" in call.args[0]
    assert call.kwargs.get("threshold") == 10


def test_seed_tweets_creates_channels_and_reports_count():
    body = {"author_username": "bench_u", "likers": [], "tweets": [],
            "channels": [{"key": "ch_00000", "name": "channel 0"},
                         {"key": "ch_00001", "name": "channel 1"}]}
    with TestClient(app) as client:
        resp = client.post("/walker/seed_tweets", json=body)
    assert resp.status_code == 200
    assert resp.json()["data"]["reports"][0]["seeded_channels"] == 2
    cyphers = " ".join(c.args[0] for c in _mock_session.run.call_args_list if c.args)
    assert ":MEMBER" in cyphers and ":Channel" in cyphers
