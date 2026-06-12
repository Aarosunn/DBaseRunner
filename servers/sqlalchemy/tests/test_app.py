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
    assert "ms_traversal" in r
    assert "ms_build_payload" in r
    assert isinstance(r["ms_traversal"], float)
    assert isinstance(r["ms_build_payload"], float)

def test_load_own_tweets_with_data(client, registered_user):
    auth = {"Authorization": f"Bearer {registered_user['username']}"}
    # Create a tweet
    client.post("/walker/create_tweet", json={"content": "hello world"}, headers=auth)
    # Load own tweets
    resp = client.post("/walker/load_own_tweets", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    tweets = body["data"]["result"]
    assert len(tweets) == 1
    t = tweets[0]
    assert t["content"] == "hello world"
    assert "id" in t
    assert "author_username" in t
    assert "created_at" in t
    assert "likes" in t
    assert "comments" in t
