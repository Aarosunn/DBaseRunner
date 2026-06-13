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
    assert "tweets" in r
    # server_timing block (fair-timing spec §3): ms_build = .report() ORM hydration tax
    st = r["server_timing"]
    assert isinstance(st["ms_fetch"], float) and st["ms_fetch"] >= 0
    assert isinstance(st["ms_build"], float) and st["ms_build"] >= 0
    assert isinstance(st["server_total"], float)
    assert st["server_total"] + 1e-6 >= st["ms_fetch"] + st["ms_build"]  # invariant

def test_load_own_tweets_applies_like_count_predicate(client, registered_user):
    # load_own_tweets now applies the benchmark predicate like_count > 10 server-side
    # (seed-design-spec §2). Seed one matching tweet and one below threshold; only the
    # matching one must come back, carrying like_count in the payload.
    auth = {"Authorization": f"Bearer {registered_user['username']}"}
    client.post("/walker/seed_tweets", json={
        "author_username": "testuser",
        "likers": ["liker_0", "liker_1"],
        "tweets": [
            {"content": "[t_0000] matching", "created_at": "2026-01-01T00:00:00Z",
             "like_count": 14, "likers": ["liker_0", "liker_1"],
             "comments": [{"author": "liker_0", "content": "nice",
                           "created_at": "2026-01-01T00:00:30Z"}]},
            {"content": "[t_0001] below threshold", "created_at": "2026-01-01T00:01:00Z",
             "like_count": 3, "likers": ["liker_0"], "comments": []},
        ],
    })
    resp = client.post("/walker/load_own_tweets", headers=auth)
    assert resp.status_code == 200
    tweets = resp.json()["data"]["result"]
    assert len(tweets) == 1                       # below-threshold tweet filtered out
    t = tweets[0]
    assert t["content"] == "[t_0000] matching"
    assert t["like_count"] == 14
    for key in ("id", "author_username", "created_at", "likes", "comments"):
        assert key in t
