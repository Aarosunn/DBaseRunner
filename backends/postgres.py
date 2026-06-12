"""Hand-tuned Postgres (FastAPI) backend adapter.

Auth tokens are bearer-username (token == username); the login/register envelope
nests under "data" (servers/postgres/src/routes/user.py).
"""

from .base import BackendBase


class PostgresBackend(BackendBase):
    def _parse_token(self, body: dict) -> str:
        return body["data"]["token"]
