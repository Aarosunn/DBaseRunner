"""Neo4j HTTP wrapper backend adapter.

Auth is bearer-username; the login/register envelope nests the token under
data.result.token (servers/neo4j/src/main.py _resp). Comments come back as JSON
strings and are parsed by base.normalize_comment.
"""

from .base import BackendBase


class Neo4jBackend(BackendBase):
    def _parse_token(self, body: dict) -> str:
        return body["data"]["result"]["token"]
