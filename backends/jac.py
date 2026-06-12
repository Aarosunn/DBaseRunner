"""Jac FULLSTACK backend adapter (served by jac-cloud as POST /walker/<name>)."""

import requests

from .base import BackendBase, seed_tweets_payload


class JacBackend(BackendBase):
    # jac-cloud auth uses an email-like field + a real JWT. jac-cloud validates
    # the email format, so the harness's plain usernames (bench_<run>_<sweep>_<param>)
    # are wrapped into a valid email. Same value for register + login (consistent
    # identity); a distinct address per param point preserves the namespacing.
    def _register_body(self, username: str, password: str) -> dict:
        return {"email": f"{username}@example.com", "password": password}

    def _parse_token(self, body: dict) -> str:
        return body["token"]

    def _extract_tweets(self, body: dict) -> list:
        # jac-cloud wraps reports: {"status":200,"reports":[{"tweets":[...]}]}.
        if "data" in body:
            return (body["data"] or {}).get("result") or []
        reports = body.get("reports") or []
        if reports and isinstance(reports[0], dict):
            return reports[0].get("tweets") or []
        return []

    def seed(self, spec: dict) -> None:
        # Identity comes from the JWT on the session — no author_username.
        body = seed_tweets_payload(spec)
        resp = self.session.post(f"{self.base_url}/walker/seed_tweets", json=body)
        resp.raise_for_status()

    def health(self) -> bool:
        # jac-cloud exposes walker:pub health as POST /walker/health, not GET.
        try:
            resp = self.session.post(
                f"{self.base_url}/walker/health", json={}, timeout=5
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def reset(self) -> None:
        # Jac has no data-delete endpoint; namespacing is the correctness mechanism
        # (harness-fix-spec §1.2). Logged no-op — never raise.
        print(
            "  [jac] reset(): no server-side data wipe; relying on eval-user namespacing"
        )

    def clear_cache(self) -> None:
        resp = self.session.post(f"{self.base_url}/walker/clear_cache", json={})
        resp.raise_for_status()
