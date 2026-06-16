"""Root-isolation regression test for the jac backend.

Guards the `:pub` → system_root collision (HARNESS_CONTEXT.md §13): when the
data-plane walkers were declared `walker:pub`, jac-cloud (filter_pushdown branch)
never extracted the JWT for them, so every bench user was spawned as GUEST on the
shared `system_root` and a freshly-registered user's `load_own_tweets` returned a
*previous* user's tweets.

The fix drops `:pub` from all data-plane walkers (keeping only `health` public),
so each authenticated request binds the caller's own root. This test proves it:

    register A -> seed A -> register fresh B -> assert B sees ZERO tweets.

Requires a live jac-cloud. Set JAC_BENCH_URL to the service base URL, e.g.:

    JAC_BENCH_URL=http://localhost:8080 uv run pytest tests/test_jac_isolation.py -q

Skipped (not failed) when JAC_BENCH_URL is unset, so it never breaks the offline
unit suite. Uses unique usernames per run so it is safe to re-run without a wipe
(jac has no clear_data).
"""

import os
import uuid

import pytest

import seed_gen
from backends import JacBackend

URL = os.environ.get("JAC_BENCH_URL")
PASSWORD = "isolpass"

pytestmark = pytest.mark.skipif(
    not URL, reason="set JAC_BENCH_URL to run the jac isolation test against a live cluster"
)


def test_fresh_user_is_root_isolated():
    """A freshly-registered user must NOT see another user's seeded tweets."""
    # Small deterministic dataset. Post predicate-drop (#9) load_own_tweets returns
    # ALL of the user's own tweets, so the expected count is n_tweets.
    spec = seed_gen.generate_point("fanout", 100)
    expected = spec["n_tweets"]
    assert expected > 0, "test dataset must contain tweets to be meaningful"

    run = uuid.uuid4().hex[:8]

    # User A: register, seed, and confirm A sees its own matching tweets.
    a = JacBackend(URL)
    a.ensure_user(f"isol_{run}_a", PASSWORD)
    a.seed(spec)
    a_tweets = a.load_own_tweets()["tweets"]
    assert len(a_tweets) == expected, (
        f"user A should see its own {expected} tweets, got {len(a_tweets)}"
    )

    # User B: register AFTER A was seeded, never seed B. With per-root isolation
    # B's own-tweets must be empty. A non-empty result == the :pub/system_root bug.
    b = JacBackend(URL)
    b.ensure_user(f"isol_{run}_b", PASSWORD)
    b_tweets = b.load_own_tweets()["tweets"]
    assert b_tweets == [], (
        f"fresh user B must see zero tweets; saw {len(b_tweets)} -> users are NOT "
        f"root-isolated (the :pub -> system_root collision has regressed)"
    )
    a.session.close()
    b.session.close()
