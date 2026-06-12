"""Hand-tuned psycopg connection pool + helpers.

Replaces SQLAlchemy entirely. Uses psycopg3's `psycopg_pool.ConnectionPool`
with prepared-statement caching enabled (`prepare_threshold=0`) so every
parameterized statement is server-side prepared after first use, giving
free plan caching with no application code.

Key design choices:

* `prepare_threshold=0` — psycopg3's prepared statement cache is enabled
  immediately on first execution rather than after some threshold. This
  is the standard "expert" knob for hot-path query workloads.

* `min_size=10, max_size=30` — pool sized to comfortably handle the
  concurrent benchmark at concurrency=20 with overhead. The fair-ORM
  baseline used `pool_size=20, max_overflow=10`; we set the upper bound
  to the same total (30) but with a higher minimum to keep more
  connections warm.

* `application_name='littlex_handtuned'` — set on every connection so
  the deployment is visible in `pg_stat_activity` for monitoring.

* `bootstrap()` runs the schema DDL exactly once at startup. Idempotent.

The walker routes never see the pool directly. Instead they call
`with_conn(fn)` which acquires/releases a connection automatically.
"""

import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/littlex"
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def make_pool():
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    # Translate the SQLAlchemy URL form `postgresql+psycopg://...` to
    # plain `postgresql://...` if anyone passes the SA-style URL.
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    print(f"[db] Using DSN: {dsn}")
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=10,
        max_size=30,
        # Server-side prepared statements after first use — psycopg's
        # equivalent of "compiled statement cache". Defaults to 5.
        kwargs={
            "prepare_threshold": 0,
            "application_name": "littlex_handtuned",
            "row_factory": dict_row,
        },
        # Don't open eagerly — wait until first use so the Flask app can
        # boot even if Postgres is still coming up. The first request
        # will block briefly while the pool primes.
        open=False,
    )
    return pool


# Module-level singleton — lazily opened by Flask app context.
_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = make_pool()
        _pool.open()
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn():
    """Context manager that acquires a connection from the pool and
    commits on success / rolls back on failure. Use this for everything.

    Usage:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT ...")
            row = cur.fetchone()
    """
    pool = get_pool()
    with pool.connection() as c:
        yield c


def bootstrap():
    """Run schema.sql once at startup. Idempotent.

    Important: schema.sql is a multi-statement script (CREATE EXTENSION,
    CREATE TABLE, CREATE INDEX, etc). Postgres cannot prepare a query that
    contains multiple commands, and our connection pool runs with
    `prepare_threshold=0` which would force every statement into a
    prepared statement. We bypass that with `prepare=False` on the
    cursor.execute() call so the DDL runs as a simple multi-statement
    batch instead.
    """
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        ddl = f.read()
    with conn() as c, c.cursor() as cur:
        cur.execute(ddl, prepare=False)
    print("[db] Schema bootstrapped.")


def reset():
    """Drop and recreate every table. Used by /walker/clear_data."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS comments, likes, follows, tweets, users CASCADE;",
            prepare=False,
        )
    bootstrap()
