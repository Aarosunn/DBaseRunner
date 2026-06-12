"""TDD: test for GET /health endpoint on the Neo4j FastAPI wrapper.

The app creates a neo4j driver at module load time and runs
ensure_constraints() on startup. We patch neo4j.GraphDatabase.driver
before the module is imported so the test never needs a live Neo4j
instance.
"""

import sys
from unittest.mock import MagicMock, patch


def _make_mock_driver():
    mock_driver = MagicMock()
    mock_session = MagicMock()
    # Support `with driver.session() as s:` context manager protocol
    mock_driver.session.return_value.__enter__ = lambda s: mock_session
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


def test_health():
    mock_driver = _make_mock_driver()

    # Patch at the neo4j package level before src.main is imported so the
    # module-level `driver = GraphDatabase.driver(...)` call is intercepted.
    with patch("neo4j.GraphDatabase.driver", return_value=mock_driver):
        # Force reimport if module was already loaded in a prior test run
        sys.modules.pop("src.main", None)

        from starlette.testclient import TestClient
        from src.main import app

        client = TestClient(app)
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
