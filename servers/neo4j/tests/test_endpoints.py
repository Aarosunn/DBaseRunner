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
