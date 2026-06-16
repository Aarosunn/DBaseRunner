"""Deterministic seed generator for the single-hop figures — SPEC_VERSION 3.

Spec: docs/specs/2026-06-13-selectivity-type-reconciliation.md §5-§6. Produces one
neutral, backend-agnostic JSON spec per sweep point plus a manifest. Same
SPEC_VERSION + same point -> byte-identical output (sha256-seeded RNG, fixed draw
order).

v3 model — the like_count>K predicate is gone (load_own_tweets returns ALL own
tweets), so:
  * fanout      : vary N tweets, no channel noise.
  * type-sel    : a mixed-type neighborhood — target tweets + Channel noise off the
                  eval user. Two modes (see SELECTIVITY_VALUES):
                    fixed-target: n_tweets=1000 const, n_channels=round(1000*(1-s)/s)
                                  -> returned set constant, isolates discrimination cost
                    fixed-total : total=1000 const, n_tweets=s*1000, n_channels=rest
                                  -> reproduces the old confounded fig6 row-for-row
  * like_count  : still seeded per-tweet (realistic 0..20 spread) for payload realism
                  and the future FP test (#21); it no longer gates the returned set.

CLI:  python seed_gen.py --out seed/
"""

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SPEC_VERSION = 3
LIKES_THRESHOLD = 10              # kept for the future FP test (#21) + run metadata
LIKER_POOL = [f"liker_{i:03d}" for i in range(32)]   # 32 distinct likers suffice
MAX_LIKES = 20                   # like_count spread is uniform [0, MAX_LIKES]

FANOUT_VALUES = [100, 250, 500, 750, 1000]

# Selectivity points per mode (spec §5). fixed-target is the default/corrected
# figure; fixed-total mirrors the published fig6 points exactly (the confound demo).
SELECTIVITY_MODES = ("fixed-target", "fixed-total")
SELECTIVITY_VALUES = {
    "fixed-target": [10, 20, 30, 50, 75, 100],
    "fixed-total":  [2, 5, 10, 20, 30, 50, 75],
}
FIXED_TARGET_N_TWEETS = 1000      # fixed-target holds the target count constant
FIXED_TOTAL_NEIGHBORHOOD = 1000   # fixed-total holds tweets+channels constant

_BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)   # base + 60s*index
_TWEET_INTERVAL = timedelta(seconds=60)
_COMMENT_INTERVAL = timedelta(seconds=30)

# Fixed word list (~200 common words, no external deps, no locale dependence) for
# deterministic content/comment text.
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


def point_dimensions(sweep_type: str, param_value: int, selectivity_mode=None):
    """Return (n_tweets, n_channels) for one sweep point (spec §5)."""
    if sweep_type == "fanout":
        return param_value, 0
    if sweep_type == "selectivity":
        pct = param_value
        if selectivity_mode == "fixed-target":
            n_tweets = FIXED_TARGET_N_TWEETS
            # n_channels = n_tweets * (1 - s) / s, integer math to avoid float drift.
            n_channels = round(n_tweets * (100 - pct) / pct)
            return n_tweets, n_channels
        if selectivity_mode == "fixed-total":
            n_tweets = round(FIXED_TOTAL_NEIGHBORHOOD * pct / 100)
            return n_tweets, FIXED_TOTAL_NEIGHBORHOOD - n_tweets
        raise ValueError(f"unknown selectivity_mode: {selectivity_mode!r}")
    raise ValueError(f"unknown sweep_type: {sweep_type!r}")


def points():
    """All (sweep_type, param_value, selectivity_mode) points this generator emits."""
    pts = [("fanout", v, None) for v in FANOUT_VALUES]
    for mode in SELECTIVITY_MODES:
        pts += [("selectivity", v, mode) for v in SELECTIVITY_VALUES[mode]]
    return pts


def _seed_string(sweep_type, param_value, selectivity_mode):
    if sweep_type == "selectivity":
        return f"v{SPEC_VERSION}:{sweep_type}:{selectivity_mode}:{param_value}"
    return f"v{SPEC_VERSION}:{sweep_type}:{param_value}"


def make_rng(sweep_type: str, param_value: int, selectivity_mode=None) -> random.Random:
    """Seed Random from sha256 of the provenance string — machine/PYTHONHASHSEED
    independent."""
    s = _seed_string(sweep_type, param_value, selectivity_mode)
    seed_int = int.from_bytes(hashlib.sha256(s.encode()).digest()[:8], "big")
    return random.Random(seed_int)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _content(rng: random.Random, key: str) -> str:
    """Deterministic ~100-140 char content prefixed with the cross-backend key."""
    s = f"[{key}] "
    while len(s) < 100:
        s += rng.choice(_WORDS) + " "
    return s.rstrip()


def _comment_text(rng: random.Random) -> str:
    """Deterministic 40-80 char comment body."""
    s = ""
    while len(s) < 40:
        s += rng.choice(_WORDS) + " "
    return s.rstrip()


def generate_point(sweep_type: str, param_value: int, selectivity_mode=None) -> dict:
    """Build one neutral seed spec (§6.1 schema). Draw order is fixed; never reorder
    draws without bumping SPEC_VERSION."""
    n_tweets, n_channels = point_dimensions(sweep_type, param_value, selectivity_mode)
    rng = make_rng(sweep_type, param_value, selectivity_mode)

    tweets = []
    for i in range(n_tweets):
        key = f"t_{i:04d}"
        # Fixed per-tweet draw order: like_count -> likers -> comments -> content.
        like_count = rng.randint(0, MAX_LIKES)
        likers = sorted(rng.sample(LIKER_POOL, min(like_count, len(LIKER_POOL))))

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

        tweets.append({
            "key": key,
            "content": _content(rng, key),
            "created_at": _iso(tweet_ts),
            "like_count": like_count,
            "likers": likers,
            "comments": comments,
        })

    # Channel noise — deterministic by index (no RNG draws), keyed cross-backend.
    channels = [{"key": f"ch_{i:05d}", "name": f"channel {i}"} for i in range(n_channels)]

    # Generator self-checks — fail loudly before emitting.
    assert len(tweets) == n_tweets
    assert len(channels) == n_channels
    assert all(len(set(t["likers"])) == len(t["likers"]) for t in tweets)
    assert all(len(t["likers"]) == t["like_count"] for t in tweets)

    selectivity_pct = param_value if sweep_type == "selectivity" else 100
    suffix = (f"selectivity_{selectivity_mode}_{param_value}"
              if sweep_type == "selectivity" else f"{sweep_type}_{param_value}")

    return {
        "spec_version": SPEC_VERSION,
        "sweep_type": sweep_type,
        "selectivity_mode": selectivity_mode,
        "param_value": param_value,
        "rng_seed_string": _seed_string(sweep_type, param_value, selectivity_mode),
        "likes_threshold": LIKES_THRESHOLD,
        "selectivity_pct": selectivity_pct,
        "n_tweets": n_tweets,
        "n_channels": n_channels,
        "channels": channels,
        "likers": LIKER_POOL,
        "eval_user_suffix": suffix,
        "tweets": tweets,
    }


def _dump(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, indent=1)


def _point_filename(sweep_type, param_value, selectivity_mode):
    if sweep_type == "selectivity":
        return f"selectivity_{selectivity_mode}_{param_value}.json"
    return f"{sweep_type}_{param_value}.json"


def write_all(out_dir: str) -> dict:
    """Write all point files + manifest.json. Returns the manifest dict."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_points = []
    for sweep_type, param_value, selectivity_mode in points():
        spec = generate_point(sweep_type, param_value, selectivity_mode)
        fname = _point_filename(sweep_type, param_value, selectivity_mode)
        (out / fname).write_text(_dump(spec))
        manifest_points.append({
            "file": fname,
            "sweep_type": sweep_type,
            "selectivity_mode": selectivity_mode,
            "param_value": param_value,
            "n_tweets": spec["n_tweets"],
            "n_channels": spec["n_channels"],
        })
    manifest = {
        "spec_version": SPEC_VERSION,
        "likes_threshold": LIKES_THRESHOLD,
        "points": manifest_points,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1))
    return manifest


def _build_parser():
    p = argparse.ArgumentParser(description="Deterministic seed generator (SPEC_VERSION 3)")
    p.add_argument("--out", default="seed/", help="Output directory (default: seed/)")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    manifest = write_all(args.out)
    print(f"Wrote {len(manifest['points'])} point files + manifest to {args.out}")


if __name__ == "__main__":
    main()
