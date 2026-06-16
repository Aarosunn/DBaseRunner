"""Abstract base + shared normalization for all LittleX benchmark backends.

One instance per benchmark run. Owns one authenticated requests.Session, exposed
as `.session`, used for ALL traffic to this backend (timed and untimed). Adapters
are CONTROL PLANE only — the timed path is a raw POST in harness.timed_call and
never goes through an adapter method (harness-fix-spec §2).
"""

import json
from abc import ABC, abstractmethod

import requests


def normalize_comment(c) -> dict:
    """Map a server comment dict (any author-key variant) to {author,content,created_at}."""
    if isinstance(c, str):
        c = json.loads(c)
    author = (c.get("author") or c.get("username")
              or c.get("author_handle") or c.get("handle"))
    return {"author": author, "content": c.get("content"), "created_at": c.get("created_at")}


def normalize_tweet(t: dict) -> dict:
    """Map one server tweet dict to the common shape (harness-fix-spec §3).

    Tolerates: missing like_count (falls back to len(likes)); comments stored as
    JSON strings (neo4j) or dicts; divergent comment author keys.
    """
    likes = list(t.get("likes") or [])
    like_count = t.get("like_count")
    if like_count is None:
        like_count = len(likes)
    comments = t.get("comments") or []
    comments = [normalize_comment(c) for c in comments]
    out = {
        "content": t.get("content"),
        "author_username": t.get("author_username"),
        "created_at": t.get("created_at"),
        "like_count": like_count,
        "likes": likes,
        "comments": comments,
    }
    if "id" in t:
        out["raw_id"] = t["id"]
    return out


def seed_tweets_payload(spec: dict) -> dict:
    """Translate the neutral seed spec into the seed_tweets body (sans identity).

    Drops the cross-backend `key` (it is embedded in `content`); keeps the liker
    pool and per-tweet content/created_at/like_count/likers/comments verbatim.
    Carries the `channels` noise array for the type-selectivity neighborhood
    (reconciliation spec §6.4); empty/absent on the fanout sweep.
    """
    keep = ("content", "created_at", "like_count", "likers", "comments")
    return {
        "likers": spec["likers"],
        "tweets": [{k: t[k] for k in keep} for t in spec["tweets"]],
        "channels": spec.get("channels", []),
    }


def extract_seeded_counts(body) -> dict:
    """Best-effort {seeded_tweets, seeded_channels} from a seed_tweets response.

    Tolerant (like extract_server_timing): digs through the jac/baseline envelopes
    and returns {} when the server doesn't self-report counts — the harness's
    channel guard then simply skips (reconciliation spec §6.4)."""
    if not isinstance(body, dict):
        return {}
    candidates = [body]
    data = body.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("result"), dict):
            candidates.append(data["result"])
        reports = data.get("reports")
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            candidates.append(reports[0])
    reports = body.get("reports")
    if isinstance(reports, list) and reports and isinstance(reports[0], dict):
        candidates.append(reports[0])
    for c in candidates:
        if "seeded_channels" in c or "seeded_tweets" in c:
            return {"seeded_tweets": c.get("seeded_tweets"),
                    "seeded_channels": c.get("seeded_channels")}
    return {}


def extract_server_timing(body: dict):
    """Pull {server_total_ms, ms_fetch, ms_build} from a load_own_tweets response.

    Best-effort, NOT fail-closed (timing is measurement; the Phase-5 content oracle
    is the fail-closed one). Returns None on any missing/malformed shape.
    """
    reports = (body.get("data") or {}).get("reports") or body.get("reports") or []
    if not reports or not isinstance(reports[0], dict):
        return None
    st = reports[0].get("server_timing")
    if not isinstance(st, dict):
        return None
    try:
        return {"server_total_ms": float(st["server_total"]),
                "ms_fetch": float(st["ms_fetch"]),
                "ms_build": float(st["ms_build"])}
    except (KeyError, TypeError, ValueError):
        return None


class BackendBase(ABC):
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self._username = None      # set by ensure_user/auth; used as seed identity

    # ── auth ──────────────────────────────────────────────────────────────────

    def _register_body(self, username: str, password: str) -> dict:
        """Body for POST /user/register. Baselines: username/password.
        JacBackend overrides with the email key."""
        return {"username": username, "password": password}

    def _login_body(self, username: str, password: str) -> dict:
        return self._register_body(username, password)

    @abstractmethod
    def _parse_token(self, body: dict) -> str:
        """Pull the auth token out of this backend's login/register response shape."""

    def ensure_user(self, username: str, password: str) -> None:
        """Register username (tolerate already-exists), then log in. On success
        self.session carries the Authorization header."""
        # Register-then-conflict is the normal idempotent path; we ignore the
        # register response (non-2xx included) and let login be the authority on
        # whether the credentials work.
        self.session.post(f"{self.base_url}/user/register",
                          json=self._register_body(username, password))
        self.auth(username, password)

    def auth(self, username: str, password: str) -> None:
        """Log in an existing user (no registration)."""
        resp = self.session.post(f"{self.base_url}/user/login",
                                 json=self._login_body(username, password))
        resp.raise_for_status()
        token = self._parse_token(resp.json())
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._username = username

    # ── data plane (all UNTIMED control plane) ────────────────────────────────

    def _extract_tweets(self, body: dict) -> list:
        """Pull the raw tweet list out of this backend's load_own_tweets envelope."""
        data = body.get("data") or {}
        return data.get("result") or []

    def load_own_tweets(self) -> dict:
        """UNTIMED fetch of the authenticated user's own tweets. POST empty body.
        Returns the normalized shape {"tweets": [...]} (harness-fix-spec §3)."""
        resp = self.session.post(f"{self.base_url}/walker/load_own_tweets", json={})
        resp.raise_for_status()
        raw = self._extract_tweets(resp.json())
        return {"tweets": [normalize_tweet(t) for t in raw]}

    def health(self) -> bool:
        """True if the backend answers its health endpoint. Default: GET /health.
        JacBackend overrides with POST /walker/health (jac-cloud serves it as a walker)."""
        try:
            resp = self.session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def seed(self, spec: dict) -> dict:
        """Load one param-point spec into the store via POST /walker/seed_tweets,
        for the currently authenticated eval user (author_username = self._username
        on baselines; identity from the JWT on jac). Returns the server-reported
        {seeded_tweets, seeded_channels} (best-effort; {} if not reported)."""
        body = seed_tweets_payload(spec)
        body["author_username"] = self._username
        resp = self.session.post(f"{self.base_url}/walker/seed_tweets", json=body)
        resp.raise_for_status()
        return extract_seeded_counts(resp.json())

    def reset(self) -> None:
        """Best-effort full data wipe via POST /walker/clear_data.
        JacBackend overrides to a logged no-op (no server support)."""
        resp = self.session.post(f"{self.base_url}/walker/clear_data", json={})
        resp.raise_for_status()

    def clear_cache(self) -> None:
        """Backend cache clear between trials. Default: no-op. JacBackend overrides.
        Only invoked in the --cold-l1 diagnostic mode (harness-fix-spec §5)."""
        return None
