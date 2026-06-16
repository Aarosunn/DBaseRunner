"""
Tests for the FastAPI SQLAlchemy backend.
These tests import the FastAPI app from src and use TestClient.
Write these tests BEFORE migrating — they will fail until FastAPI is implemented.
"""
import pytest
from starlette.testclient import TestClient

# Override DATABASE_URL to use SQLite in-memory for tests
import os
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

from src import app, get_db, Base
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# In-memory SQLite engine for tests.
# StaticPool ensures that all sessions share the same in-memory connection so
# that tables created by create_all() are visible to every subsequent execute().
# Without it, each pool checkout may open a fresh connection with an empty DB.
TEST_ENGINE = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)

def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=TEST_ENGINE)
    app.dependency_overrides[get_db] = override_get_db
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)
    app.dependency_overrides.clear()

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def registered_user(client):
    resp = client.post("/user/register", json={"username": "testuser", "password": "pass"})
    assert resp.status_code == 200
    return resp.json()["data"]

# ── Health ──────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

# ── Auth ─────────────────────────────────────────────────────────────────────

def test_load_own_tweets_no_auth(client):
    resp = client.post("/walker/load_own_tweets")
    assert resp.status_code == 401

def test_load_own_tweets_bad_user(client):
    resp = client.post(
        "/walker/load_own_tweets",
        headers={"Authorization": "Bearer nonexistent"}
    )
    assert resp.status_code == 400

# ── User registration / login ────────────────────────────────────────────────

def test_register(client):
    resp = client.post("/user/register", json={"username": "u1", "password": "p1"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "u1"
    assert "token" in data
    assert "root_id" in data

def test_login(client, registered_user):
    resp = client.post("/user/login", json={"username": "testuser", "password": "pass"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "testuser"

def test_login_wrong_password(client, registered_user):
    resp = client.post("/user/login", json={"username": "testuser", "password": "wrong"})
    assert resp.status_code == 400

# ── load_own_tweets ──────────────────────────────────────────────────────────

def test_load_own_tweets_empty(client, registered_user):
    resp = client.post(
        "/walker/load_own_tweets",
        headers={"Authorization": f"Bearer {registered_user['username']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "result" in body["data"]
    assert "reports" in body["data"]
    assert isinstance(body["data"]["result"], list)
    reports = body["data"]["reports"]
    assert len(reports) == 1
    r = reports[0]
    # envelope dedup (ledger #3): tweets live ONCE, in data.result — not in the report.
    assert "tweets" not in r
    # server_timing block (fair-timing spec §3): ms_build = .report() ORM hydration tax
    st = r["server_timing"]
    assert isinstance(st["ms_fetch"], float) and st["ms_fetch"] >= 0
    assert isinstance(st["ms_build"], float) and st["ms_build"] >= 0
    assert isinstance(st["server_total"], float)
    assert st["server_total"] + 1e-6 >= st["ms_fetch"] + st["ms_build"]  # invariant

def _seed_two_tweets(client):
    client.post("/walker/seed_tweets", json={
        "author_username": "testuser",
        "likers": ["liker_0", "liker_1"],
        "tweets": [
            {"content": "[t_0000] high likes", "created_at": "2026-01-01T00:00:00Z",
             "like_count": 14, "likers": ["liker_0", "liker_1"],
             "comments": [{"author": "liker_0", "content": "nice",
                           "created_at": "2026-01-01T00:00:30Z"}]},
            {"content": "[t_0001] low likes", "created_at": "2026-01-01T00:01:00Z",
             "like_count": 3, "likers": ["liker_0"], "comments": []},
        ],
    })


def test_load_own_tweets_returns_all_own_tweets(client, registered_user):
    # Reconciliation spec §4: the like_count>10 predicate is GONE — load_own_tweets
    # returns ALL of the caller's tweets regardless of like_count.
    auth = {"Authorization": f"Bearer {registered_user['username']}"}
    _seed_two_tweets(client)
    resp = client.post("/walker/load_own_tweets", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    tweets = body["data"]["result"]
    assert len(tweets) == 2                       # both returned, no predicate
    # single-copy envelope (ledger #3): the report carries no second copy
    assert "tweets" not in body["data"]["reports"][0]
    contents = {t["content"] for t in tweets}
    assert contents == {"[t_0000] high likes", "[t_0001] low likes"}
    for t in tweets:
        for key in ("id", "author_username", "created_at", "like_count", "likes", "comments"):
            assert key in t


def test_load_own_tweets_threshold_param_filters(client, registered_user):
    # FP seam (spec §10): an explicit threshold filters server-side. Default off.
    auth = {"Authorization": f"Bearer {registered_user['username']}"}
    _seed_two_tweets(client)
    resp = client.post("/walker/load_own_tweets", headers=auth, json={"threshold": 10})
    assert resp.status_code == 200
    tweets = resp.json()["data"]["result"]
    assert len(tweets) == 1
    assert tweets[0]["content"] == "[t_0000] high likes"


def test_seed_tweets_creates_channels_and_reports_count(client, registered_user):
    # Channel noise for the type-selectivity neighborhood (spec §6.4): seeded but
    # never returned by load_own_tweets (TPT pre-separates the type).
    auth = {"Authorization": f"Bearer {registered_user['username']}"}
    resp = client.post("/walker/seed_tweets", json={
        "author_username": "testuser",
        "likers": [],
        "tweets": [],
        "channels": [{"key": "ch_00000", "name": "channel 0"},
                     {"key": "ch_00001", "name": "channel 1"},
                     {"key": "ch_00002", "name": "channel 2"}],
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["reports"][0]["seeded_channels"] == 3
    # load_own_tweets must NOT return channels
    load = client.post("/walker/load_own_tweets", headers=auth)
    assert load.json()["data"]["result"] == []
