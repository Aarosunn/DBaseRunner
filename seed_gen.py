"""Deterministic single-hop seed generator for Figures 5/6.

Spec: docs/specs/seed-design-spec.md §2-§5. Produces one neutral, backend-agnostic
JSON spec per sweep point plus a manifest. Same SPEC_VERSION + same point ->
byte-identical output (sha256-seeded RNG, fixed draw order).

CLI:  python seed_gen.py --out seed/
"""

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SPEC_VERSION = 2
LIKES_THRESHOLD = 10              # predicate is like_count > K (§2)
LIKER_POOL = [f"liker_{i:03d}" for i in range(32)]   # §2: 32 distinct likers suffice

FANOUT_VALUES = [100, 250, 500, 750, 1000]
SELECTIVITY_VALUES = [10, 25, 50, 75, 100]
# fanout sweep fixes selectivity so the lowest point (fanout=100) still yields
# >= ~20-30 target tweets (HARNESS_CONTEXT.md §8 floor); 25% -> 25 at fanout=100.
FANOUT_SELECTIVITY_PCT = 25
SELECTIVITY_N_TWEETS = 1000       # selectivity sweep fixes fan-out at 1000 (§5)

_BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)   # §3: base + 60s*index
_TWEET_INTERVAL = timedelta(seconds=60)
_COMMENT_INTERVAL = timedelta(seconds=30)

# Fixed word list (~200 common words, no external deps, no locale dependence) for
# deterministic content/comment text (§4).
_WORDS = (
    "the be to of and a in that have it for not on with as you do at this but his by "
    "from they we say her she or an will my one all would there their what so up out if "
    "about who get which go me when make can like time no just him know take people into "
    "year your good some could them see other than then now look only come its over think "
    "also back after use two how our work first well way even new want because any these "
    "give day most us graph node edge query data store index field user tweet like comment "
    "fast slow load read write fetch value count list set map cache warm cold trial point "
    "sweep latency bytes server client request response auth token round hop depth scope "
    "small large total result match band pool name text label record system model native"
).split()


def expected_matching(n_tweets: int, selectivity_pct: int) -> int:
    """Exact count of tweets matching the predicate (§4: round-half-up)."""
    return (n_tweets * selectivity_pct + 50) // 100


def point_dimensions(sweep_type: str, param_value: int):
    """Return (n_tweets, selectivity_pct, n_matching) for one sweep point."""
    if sweep_type == "fanout":
        n_tweets = param_value
        selectivity_pct = FANOUT_SELECTIVITY_PCT
    elif sweep_type == "selectivity":
        n_tweets = SELECTIVITY_N_TWEETS
        selectivity_pct = param_value
    else:
        raise ValueError(f"unknown sweep_type: {sweep_type!r}")
    return n_tweets, selectivity_pct, expected_matching(n_tweets, selectivity_pct)


def points():
    """All (sweep_type, param_value) points this generator emits."""
    return ([("fanout", v) for v in FANOUT_VALUES]
            + [("selectivity", v) for v in SELECTIVITY_VALUES])


def make_rng(sweep_type: str, param_value: int) -> random.Random:
    """Seed Random from sha256 of the provenance string — machine/PYTHONHASHSEED
    independent (§4)."""
    s = f"v{SPEC_VERSION}:{sweep_type}:{param_value}"
    seed_int = int.from_bytes(hashlib.sha256(s.encode()).digest()[:8], "big")
    return random.Random(seed_int)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _content(rng: random.Random, key: str) -> str:
    """Deterministic ~100-140 char content prefixed with the cross-backend key (§3)."""
    s = f"[{key}] "
    while len(s) < 100:
        s += rng.choice(_WORDS) + " "
    return s.rstrip()


def _comment_text(rng: random.Random) -> str:
    """Deterministic 40-80 char comment body (§3)."""
    s = ""
    while len(s) < 40:
        s += rng.choice(_WORDS) + " "
    return s.rstrip()


def generate_point(sweep_type: str, param_value: int) -> dict:
    """Build one neutral seed spec (§3 schema). Draw order is fixed; never reorder
    draws without bumping SPEC_VERSION."""
    n_tweets, selectivity_pct, n_matching = point_dimensions(sweep_type, param_value)
    rng = make_rng(sweep_type, param_value)

    # Matching tweets scattered through the timeline (§4 step 3): drawn first.
    matching_idx = set(rng.sample(range(n_tweets), n_matching))

    tweets = []
    matching_keys = []
    for i in range(n_tweets):
        key = f"t_{i:04d}"
        is_match = i in matching_idx
        # Fixed per-tweet draw order: like_count -> likers -> comments -> content.
        like_count = rng.randint(11, 20) if is_match else rng.randint(0, 10)
        likers = sorted(rng.sample(LIKER_POOL, like_count))

        tweet_ts = _BASE_TS + _TWEET_INTERVAL * i
        n_comments = rng.randint(0, 3)
        comments = []
        for j in range(n_comments):
            author = rng.choice(LIKER_POOL)
            text = _comment_text(rng)
            comments.append({
                "author": author,
                "content": text,
                "created_at": _iso(tweet_ts + _COMMENT_INTERVAL * (j + 1)),
            })

        content = _content(rng, key)
        if is_match:
            matching_keys.append(key)
        tweets.append({
            "key": key,
            "content": content,
            "created_at": _iso(tweet_ts),
            "like_count": like_count,
            "likers": likers,
            "comments": comments,
        })

    # Generator self-checks (§4) — fail loudly before emitting.
    assert len(matching_keys) == n_matching
    assert all(t["like_count"] > LIKES_THRESHOLD for t in tweets if t["key"] in set(matching_keys))
    assert all(t["like_count"] <= LIKES_THRESHOLD for t in tweets if t["key"] not in set(matching_keys))
    assert all(len(set(t["likers"])) == len(t["likers"]) for t in tweets)

    return {
        "spec_version": SPEC_VERSION,
        "sweep_type": sweep_type,
        "param_value": param_value,
        "rng_seed_string": f"v{SPEC_VERSION}:{sweep_type}:{param_value}",
        "likes_threshold": LIKES_THRESHOLD,
        "n_tweets": n_tweets,
        "selectivity_pct": selectivity_pct,
        "expected_matching": n_matching,
        "expected_matching_keys": matching_keys,
        "likers": LIKER_POOL,
        "eval_user_suffix": f"{sweep_type}_{param_value}",
        "tweets": tweets,
    }


def _dump(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, indent=1)


def write_all(out_dir: str) -> dict:
    """Write all point files + manifest.json. Returns the manifest dict."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_points = []
    for sweep_type, param_value in points():
        spec = generate_point(sweep_type, param_value)
        fname = f"{sweep_type}_{param_value}.json"
        (out / fname).write_text(_dump(spec))
        manifest_points.append({
            "file": fname,
            "sweep_type": sweep_type,
            "param_value": param_value,
            "expected_matching": spec["expected_matching"],
        })
    manifest = {
        "spec_version": SPEC_VERSION,
        "likes_threshold": LIKES_THRESHOLD,
        "points": manifest_points,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1))
    return manifest


def _build_parser():
    p = argparse.ArgumentParser(description="Deterministic seed generator (Figs 5/6)")
    p.add_argument("--out", default="seed/", help="Output directory (default: seed/)")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    manifest = write_all(args.out)
    print(f"Wrote {len(manifest['points'])} point files + manifest to {args.out}")


if __name__ == "__main__":
    main()
