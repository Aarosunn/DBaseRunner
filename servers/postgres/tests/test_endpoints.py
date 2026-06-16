"""TDD tests for the FastAPI Postgres server.

Written BEFORE any implementation (RED phase).
Run with: pytest tests/test_endpoints.py -v

All DB calls are mocked so no live Postgres is needed.
"""
import sys
import os
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the package root is on sys.path so `from src import ...` works
# when pytest is run from DBaseRunner/servers/postgres/.
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# We need to prevent db.bootstrap() (which talks to a real Postgres) from
# running during test collection/import.  Patch it before importing src.
# ---------------------------------------------------------------------------
with patch("psycopg_pool.ConnectionPool"):
    from src import app
    from src.routes.walker import get_current_user

from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_cursor(fetchone_return=None, fetchall_return=None):
    """Return a mock cursor usable as a context manager."""
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_return
    cur.fetchall.return_value = fetchall_return or []
    # support `with c.cursor() as cur:`
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _make_mock_conn(cursor):
    """Return a mock connection usable as `with db.conn() as c, c.cursor() as cur:`."""
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


@contextmanager
def _patched_conn(fetchone_return=None, fetchall_return=None):
    """Context manager that patches src.db.conn with a mock."""
    cur = _make_mock_cursor(fetchone_return=fetchone_return,
                            fetchall_return=fetchall_return)
    conn = _make_mock_conn(cur)

    @contextmanager
    def mock_conn():
        yield conn

    with patch("src.db.conn", mock_conn):
        yield cur


# ---------------------------------------------------------------------------
# Test 1 — GET /health → 200, body {"status": "ok"}, no auth required
# ---------------------------------------------------------------------------

def test_health_no_auth():
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Test 2 — POST /walker/load_own_tweets without Authorization → 401
# ---------------------------------------------------------------------------

def test_load_own_tweets_missing_auth_returns_401():
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/walker/load_own_tweets", json={})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 3 — POST /walker/load_own_tweets with mocked user → correct schema
# ---------------------------------------------------------------------------

MOCK_USER = {"id": 1, "username": "testuser", "handle": "testuser", "bio": ""}

MOCK_TWEETS = [
    {
        "id": 42,
        "content": "hello world",
        "author_username": "testuser",
        "created_at": "2026-01-01T00:00:00",
        "likes": [],
        "comments": [],
    }
]


def test_load_own_tweets_with_mock_user_returns_correct_schema():
    # Override the auth dependency so the route sees a pre-populated user.
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER

    try:
        # Mock the DB call inside the route: cursor.fetchone() returns a
        # dict with key "tweets" matching the SQL column alias.
        with _patched_conn(fetchone_return={"tweets": MOCK_TWEETS}):
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.post("/walker/load_own_tweets", json={})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    body = resp.json()

    # Top-level shape
    assert "data" in body
    data = body["data"]
    assert "result" in data
    assert "reports" in data

    # reports[0] shape
    reports = data["reports"]
    assert isinstance(reports, list) and len(reports) == 1
    report = reports[0]
    # envelope dedup (ledger #3): tweets live ONCE, in data.result — NOT also in
    # the report. reports[0] keeps only server_timing.
    assert "tweets" not in report
    assert data["result"] == MOCK_TWEETS
    # server_timing block (fair-timing spec §3)
    st = report["server_timing"]
    assert isinstance(st["ms_fetch"], float) and st["ms_fetch"] >= 0
    assert st["ms_build"] == 0.0  # PG builds in-SQL, inside ms_fetch
    assert isinstance(st["server_total"], float)
    assert st["server_total"] + 1e-6 >= st["ms_fetch"] + st["ms_build"]  # invariant


# ---------------------------------------------------------------------------
# Test 4 — POST /user/register → body contains "token"
# ---------------------------------------------------------------------------

def test_register_returns_token():
    # Mock the DB: INSERT RETURNING id → {"id": 99}
    with _patched_conn(fetchone_return={"id": 99}):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/user/register",
                json={"username": "u", "password": "p"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body.get("data", {})


# ---------------------------------------------------------------------------
# Type-selectivity reconciliation (spec §4, §10): the like_count>10 predicate is
# DROPPED from load_own_tweets (returns ALL own tweets); an optional `threshold`
# body param is the FP seam (default off).
# ---------------------------------------------------------------------------

def _capture_load_sql(body):
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    try:
        with _patched_conn(fetchone_return={"tweets": []}) as cur:
            with TestClient(app, raise_server_exceptions=True) as client:
                client.post("/walker/load_own_tweets", json=body)
        # the load query is the execute call selecting FROM tweets
        for c in cur.execute.call_args_list:
            sql = c.args[0]
            if "FROM tweets" in sql:
                return sql, c.args[1] if len(c.args) > 1 else ()
        raise AssertionError("no load query executed")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_load_own_tweets_default_has_no_like_count_predicate():
    sql, params = _capture_load_sql({})
    assert "like_count >" not in sql
    assert tuple(params) == (MOCK_USER["id"],)


def test_load_own_tweets_threshold_param_adds_predicate():
    sql, params = _capture_load_sql({"threshold": 10})
    assert "like_count >" in sql
    assert 10 in tuple(params)


# ---------------------------------------------------------------------------
# seed_tweets creates Channel noise + channel_members and self-reports the count
# (reconciliation spec §6.4).
# ---------------------------------------------------------------------------

def test_seed_tweets_creates_channels_and_reports_count():
    body = {
        "author_username": "bench_u",
        "likers": [],
        "tweets": [],
        "channels": [{"key": "ch_00000", "name": "channel 0"},
                     {"key": "ch_00001", "name": "channel 1"}],
    }
    # _upsert_user / channel INSERT ... RETURNING id all fetchone() -> give an id
    with _patched_conn(fetchone_return={"id": 5}) as cur:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/walker/seed_tweets", json=body)
    assert resp.status_code == 200
    report = resp.json()["data"]["reports"][0]
    assert report["seeded_channels"] == 2
    # a channel_members insert ran
    joined = " ".join(c.args[0] for c in cur.execute.call_args_list
                      if c.args) + " ".join(
        c.args[0] for c in cur.executemany.call_args_list if c.args)
    assert "channel_members" in joined
