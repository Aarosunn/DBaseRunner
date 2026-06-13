"""Unified benchmark harness for JacDB.

Methodology decisions (all locked in):
  TIMING:   Client-side perf_counter wrapping the full HTTP POST — no server fields.
            The timed request is POST /walker/load_own_tweets with an EMPTY payload
            on every backend and every sweep point; identity is the Authorization
            header. raise_for_status() is outside the timer (pure Python, no I/O).
  SWEEP:    A sweep point is a DATASET, not a request parameter. For each
            (sweep_type, param_value) the harness creates a fresh eval user, seeds
            exactly that point's data (deterministic spec from seed/), verifies the
            seed landed, then runs warmup + timed trials. See seed-design-spec.md.
  CACHE:    ALL WARM by default. 20-request warmup conditions L1 like the baselines'
            buffer pools; NO clear_cache between trials. --cold-l1 is an opt-in,
            jac-only diagnostic that clears L1 before each trial (recorded in the
            metadata sidecar so its rows are never mistaken for headline figures).
  WARMUP:   20 requests per config point. Warmup rows written (warmup=1).
            plot.py filters warmup=0 only.
  SESSION:  One persistent requests.Session per backend run (owned by the adapter,
            exposed as backend.session), used for ALL traffic — timed and untimed.
  CSV:      One row per trial, warmup and timed alike. No pre-aggregation.
"""

import argparse
import csv
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import seed_gen

CSV_FIELDNAMES = [
    "backend",
    "sweep_type",
    "param_value",
    "trial_num",
    "latency_ms",
    "response_bytes",
    "timestamp",
    "warmup",
]

# Default sweeps run by --sweep. hop_depth is Phase 4.5 (load_extended_feed) and is
# deliberately NOT here — it requires a follows graph (seed-design-spec §10).
SWEEPS = {
    "fanout":      [100, 250, 500, 750, 1000],
    "selectivity": [10, 25, 50, 75, 100],
}

# Reserved for Phase 4.5 — do not implement here (HARNESS_REBUILD Phase 4.5).
PHASE_4_5_SWEEPS = {"hop_depth": [1, 2, 3]}

# Endpoint per sweep type; keeps the future multi-hop seam out of per-sweep ifs.
SWEEP_ENDPOINTS = {
    "fanout":      "load_own_tweets",
    "selectivity": "load_own_tweets",
    "hop_depth":   "load_extended_feed",   # Phase 4.5, guarded in main()
}

TRIALS = 30
WARMUP_COUNT = 20

_KEY_RE = re.compile(r"^\[(t_\d+)\]")


def timed_call(session, url, payload):
    """Client-side perf_counter wrapping the full HTTP POST.

    Returns (latency_ms: float, response_bytes: int).
    raise_for_status is outside the timer — pure Python, no I/O.
    """
    t0 = time.perf_counter()
    resp = session.post(url, json=payload)
    t1 = time.perf_counter()
    resp.raise_for_status()
    return (t1 - t0) * 1000, len(resp.content)


def run_sweep(
    backend_name,
    sweep_type,
    param_values,
    timed_fn,
    writer,
    warmup_count=WARMUP_COUNT,
    trials=TRIALS,
    clear_fn=None,
    timestamp_fn=None,
    setup_fn=None,
):
    """Run warmup + timed trials for one sweep type, writing one CSV row per call.

    Args:
        backend_name:  string label written to every row (e.g. "jac")
        sweep_type:    "fanout" | "selectivity"
        param_values:  list of parameter values to sweep over
        timed_fn:      callable(param_value) -> (latency_ms, response_bytes)
        writer:        csv.DictWriter — must already have headers written
        warmup_count:  requests fired before timed trials (warmup=1 rows)
        trials:        timed requests per param_value (warmup=0 rows)
        clear_fn:      callable() -> None, called before each timed trial.
                       Only set in --cold-l1 mode (jac). None = all-warm.
        timestamp_fn:  injectable for tests; defaults to time.time
        setup_fn:      callable(param_value) -> None, called ONCE per param point
                       before that point's first warmup row (re-seed the dataset).
                       Setup time is never written to the CSV. None = no setup.
    """
    if timestamp_fn is None:
        timestamp_fn = time.time

    for param_value in param_values:
        if setup_fn is not None:
            setup_fn(param_value)

        for i in range(warmup_count):
            latency_ms, response_bytes = timed_fn(param_value)
            writer.writerow({
                "backend": backend_name,
                "sweep_type": sweep_type,
                "param_value": param_value,
                "trial_num": i,
                "latency_ms": round(latency_ms, 3),
                "response_bytes": response_bytes,
                "timestamp": timestamp_fn(),
                "warmup": 1,
            })

        for i in range(trials):
            if clear_fn is not None:
                clear_fn()
            latency_ms, response_bytes = timed_fn(param_value)
            writer.writerow({
                "backend": backend_name,
                "sweep_type": sweep_type,
                "param_value": param_value,
                "trial_num": i,
                "latency_ms": round(latency_ms, 3),
                "response_bytes": response_bytes,
                "timestamp": timestamp_fn(),
                "warmup": 0,
            })


# ── seed/verify control plane (untimed) ───────────────────────────────────────

def check_sweep_supported(sweep_type):
    """Guard hop_depth out of the implemented path (harness-fix-spec §4)."""
    if sweep_type in PHASE_4_5_SWEEPS:
        raise SystemExit(
            "hop_depth sweep is Phase 4.5 (load_extended_feed); not implemented. "
            "Remove it from --sweep.")


def load_point_spec(seed_dir, sweep_type, param_value):
    """Load one pre-generated neutral seed spec (seed-design-spec §3)."""
    path = Path(seed_dir) / f"{sweep_type}_{param_value}.json"
    if not path.exists():
        raise SystemExit(
            f"seed spec not found: {path}; run `python seed_gen.py --out {seed_dir}`")
    return json.loads(path.read_text())


def _content_key(content):
    m = _KEY_RE.match(content or "")
    return m.group(1) if m else None


def guard_not_already_seeded(backend, username):
    """Hard-fail if the eval user already has data (harness-fix-spec §1.3)."""
    result = backend.load_own_tweets()
    if result["tweets"]:
        raise SystemExit(
            f"eval user {username} already has data; pass a fresh --run-id, "
            f"or --skip-seed to reuse the existing dataset.")


def verify_seed(backend, spec):
    """Hard-fail post-seed verification (harness-fix-spec §1.4): exact matching
    count, every returned tweet above threshold, exact matching-key set."""
    tweets = backend.load_own_tweets()["tweets"]
    expected = spec["expected_matching"]
    if len(tweets) != expected:
        raise SystemExit(
            f"seed verify failed: expected {expected} matching tweets, "
            f"got {len(tweets)}")

    threshold = spec["likes_threshold"]
    # The liker pool caps how many distinct likes a tweet can carry. The seed
    # generator guarantees len(likers) == min(like_count, pool_size); with no
    # pool in the spec, fall back to like_count (no cap).
    pool_size = len(spec.get("likers") or [])
    for t in tweets:
        if not t["like_count"] > threshold:
            raise SystemExit(
                f"seed verify failed: tweet like_count {t['like_count']} "
                f"is not > threshold {threshold}")
        expected_likes = min(t["like_count"], pool_size) if pool_size else t["like_count"]
        if len(t["likes"]) != expected_likes:
            raise SystemExit(
                f"seed verify failed: tweet like_count {t['like_count']} but got "
                f"{len(t['likes'])} likes (expected {expected_likes}) — thin/empty "
                f"likes payload (jac detached-liker risk, HARNESS_REVIEW H1)")

    got_keys = {_content_key(t["content"]) for t in tweets}
    expected_keys = set(spec["expected_matching_keys"])
    if got_keys != expected_keys:
        missing = expected_keys - got_keys
        extra = got_keys - expected_keys
        raise SystemExit(
            f"seed verify failed: key set mismatch (missing={sorted(missing)[:5]}, "
            f"extra={sorted(str(e) for e in extra)[:5]})")


def _git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def write_run_metadata(out_dir, args, *, started_at, finished_at, harness_git_sha,
                       cold_l1):
    """Write results/{backend}_meta.json beside the frozen-schema CSV (§1.7).
    Phase 5 joins backends on run_id; the defense pins likes_threshold + spec
    version to each figure."""
    meta = {
        "run_id": args.run_id,
        "backend": args.backend,
        "harness_git_sha": harness_git_sha,
        "seed_spec_version": seed_gen.SPEC_VERSION,
        "likes_threshold": seed_gen.LIKES_THRESHOLD,
        "warmup": args.warmup,
        "trials": args.trials,
        "sweeps": list(args.sweep),
        "cold_l1": cold_l1,
        "started_at": started_at,
        "finished_at": finished_at,
        "notes": {"jac_topology_index": "from k8s env JAC_TOPOLOGY_INDEX, record actual value"},
    }
    (Path(out_dir) / f"{args.backend}_meta.json").write_text(json.dumps(meta, indent=2))


def _build_parser():
    p = argparse.ArgumentParser(description="JacDB benchmark harness")
    p.add_argument("--backend", required=True,
                   choices=["jac", "postgres", "sqlalchemy", "neo4j"],
                   help="Backend to benchmark")
    p.add_argument("--url", required=True, help="Base URL of the backend service")
    p.add_argument("--user", default="bench@example.com",
                   help="Registration email/username source for eval users")
    p.add_argument("--password", default="benchpass", help="Eval-user password")
    p.add_argument("--run-id", dest="run_id", default=None,
                   help="Run identifier; eval users are bench_<run_id>_<sweep>_<param>. "
                        "Phase 5 requires the SAME --run-id across all four backend "
                        "runs so seeded data matches. Default: harness start timestamp.")
    p.add_argument("--seed-dir", dest="seed_dir", default="seed/",
                   help="Directory of pre-generated seed specs (default: seed/)")
    p.add_argument("--skip-seed", dest="skip_seed", action="store_true",
                   help="Skip register+seed; go straight to verify+timing against an "
                        "already-seeded run (reuse a prior --run-id).")
    p.add_argument("--reset", action="store_true",
                   help="POST clear_data once at run start (PG/SQLA/neo4j only; jac no-op).")
    p.add_argument("--cold-l1", dest="cold_l1", action="store_true",
                   help="DIAGNOSTIC: clear jac L1 before each trial (jac only). "
                        "Default off — headline runs are all-warm.")
    # hop_depth is in choices so OUR Phase-4.5 error fires, not argparse's.
    p.add_argument("--sweep", nargs="+",
                   choices=list(SWEEPS.keys()) + list(PHASE_4_5_SWEEPS.keys()),
                   default=list(SWEEPS.keys()),
                   help="Sweep types to run (default: fanout selectivity)")
    p.add_argument("--trials", type=int, default=TRIALS)
    p.add_argument("--warmup", type=int, default=WARMUP_COUNT)
    p.add_argument("--out", default="results",
                   help="Directory to write CSVs (default: results/)")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.run_id is None:
        args.run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    # Fail fast on an unsupported sweep BEFORE constructing anything.
    for sweep_type in args.sweep:
        check_sweep_supported(sweep_type)

    from backends import JacBackend, PostgresBackend, SQLAlchemyBackend, Neo4jBackend
    backend_classes = {
        "jac":        JacBackend,
        "postgres":   PostgresBackend,
        "sqlalchemy": SQLAlchemyBackend,
        "neo4j":      Neo4jBackend,
    }
    backend = backend_classes[args.backend](args.url)
    if not backend.health():
        raise SystemExit(f"{args.backend} health check failed at {args.url}")
    if args.reset:
        backend.reset()

    # All-warm by default; --cold-l1 wires the jac-only per-trial L1 clear (§5).
    clear_fn = None
    if args.cold_l1 and args.backend == "jac":
        clear_fn = backend.clear_cache

    load_url = f"{args.url.rstrip('/')}/walker/load_own_tweets"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.backend}.csv"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()

        for sweep_type in args.sweep:
            param_values = SWEEPS[sweep_type]

            def setup_fn(param_value, _sweep=sweep_type):
                spec = load_point_spec(args.seed_dir, _sweep, param_value)
                username = f"bench_{args.run_id}_{_sweep}_{param_value}"
                if args.skip_seed:
                    backend.auth(username, args.password)
                else:
                    backend.ensure_user(username, args.password)
                    guard_not_already_seeded(backend, username)
                    backend.seed(spec)
                verify_seed(backend, spec)

            def timed_fn(param_value):
                # Identity is the Authorization header set in setup_fn; payload is {}.
                return timed_call(backend.session, load_url, {})

            run_sweep(
                args.backend, sweep_type, param_values,
                timed_fn, writer,
                warmup_count=args.warmup,
                trials=args.trials,
                clear_fn=clear_fn,
                setup_fn=setup_fn,
            )
            print(f"  {sweep_type}: done", flush=True)

    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_metadata(out_dir, args, started_at=started_at, finished_at=finished_at,
                       harness_git_sha=_git_sha(), cold_l1=bool(args.cold_l1))

    print(f"Results written to {out_path}")
    backend.session.close()


if __name__ == "__main__":
    main()
