"""SQLAlchemy (FastAPI) backend adapter.

Same auth envelope as Postgres: token == username, nested under "data"
(servers/sqlalchemy/src/routes/user.py).
"""

from .base import BackendBase


class SQLAlchemyBackend(BackendBase):
    def _parse_token(self, body: dict) -> str:
        return body["data"]["token"]
