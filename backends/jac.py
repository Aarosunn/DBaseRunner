"""Jac FULLSTACK backend adapter."""

import requests
from .base import BackendBase


class JacBackend(BackendBase):
    def __init__(self, base_url: str):
        super().__init__(base_url)
        self._session = requests.Session()
        self._token: str | None = None

    def auth(self, username: str, password: str) -> None:
        resp = self._session.post(
            f"{self.base_url}/user/login",
            json={"email": username, "password": password},
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        self._session.headers["Authorization"] = f"Bearer {self._token}"

    def load_own_tweets(self, user_id: str, limit: int, selectivity: int) -> dict:
        resp = self._session.post(
            f"{self.base_url}/walker/load_own_tweets",
            json={"user_id": user_id, "limit": limit, "selectivity": selectivity},
        )
        resp.raise_for_status()
        return self._normalize(resp.json())

    @staticmethod
    def _normalize(body: dict) -> dict:
        """Normalize jac-cloud's walker envelope to the common backend shape.

        jac-cloud serves walkers as POST /walker/<name> and wraps `report`
        values as {"status": 200, "reports": [<report>...]}, with no "data"
        or "result" wrapper. The baselines return
        {"data": {"result": [...tweets], "reports": [{...}]}}. Map jac-cloud
        onto that shape so the harness/correctness check sees one format.
        """
        if "data" in body:  # already common shape
            return body
        reports = body.get("reports") or []
        report = reports[0] if reports and isinstance(reports[0], dict) else {}
        tweets = report.get("tweets", [])
        return {"data": {"result": tweets, "reports": reports}}

    def health(self) -> bool:
        # jac-cloud exposes walker:pub health as POST /walker/health, not GET.
        try:
            resp = self._session.post(
                f"{self.base_url}/walker/health", json={}, timeout=5
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def seed(self, dataset_path: str) -> None:
        raise NotImplementedError("Jac seed not yet implemented")
