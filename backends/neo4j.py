"""Neo4j HTTP wrapper backend adapter."""

import requests
from .base import BackendBase


class Neo4jBackend(BackendBase):
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
        return resp.json()

    def health(self) -> bool:
        try:
            resp = self._session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def seed(self, dataset_path: str) -> None:
        raise NotImplementedError("Neo4j seed not yet implemented")
