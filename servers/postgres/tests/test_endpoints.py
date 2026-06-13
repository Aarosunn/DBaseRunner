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
    assert "tweets" in report
    assert isinstance(report["tweets"], list)
    # server_timing block (fair-timing spec §3)
    st = report["server_timing"]
    assert isinstance(st["ms_fetch"], float) and st["ms_fetch"] >= 0
    assert st["ms_build"] == 0.0  # PG builds in-SQL, inside ms_fetch
    assert isinstance(st["server_total"], float)
    assert st["server_total"] + 1e-6 >= st["ms_fetch"] + st["ms_build"]  # invariant

    # result is the tweets list
    assert isinstance(data["result"], list)


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
