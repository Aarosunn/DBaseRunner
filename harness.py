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
from backends.base import extract_server_timing

CSV_FIELDNAMES = [
    "backend",
    "sweep_type",
    "selectivity_mode",  # "fixed-target" | "fixed-total" for selectivity; blank otherwise
    "param_value",
    "trial_num",
    "latency_ms",        # client_total
    "server_total_ms",   # handler entry → return (substrate, network-excluded)
    "ms_fetch",          # server sub-phase (descriptive, NOT cross-comparable)
    "ms_build",          # server sub-phase (descriptive, NOT cross-comparable)
    "network_ms",        # = latency_ms − server_total_ms; transport+framework+AUTH; provisional
    "response_bytes",
    "timestamp",
    "warmup",
]

# Selectivity points are MODE-dependent (selectivity-type-reconciliation spec §5):
# fixed-target (default) holds n_tweets=1000; fixed-total mirrors the published
# fig6 points exactly. The actual list for a run is chosen by --selectivity-mode.
SELECTIVITY_POINTS = {
    "fixed-target": [10, 20, 30, 50, 75, 100],
    "fixed-total":  [2, 5, 10, 20, 30, 50, 75],
}
DEFAULT_SELECTIVITY_MODE = "fixed-target"

# Default sweeps run by --sweep. hop_depth is Phase 4.5 (load_extended_feed) and is
# deliberately NOT here — it requires a follows graph (seed-design-spec §10). The
# selectivity entry carries the default-mode points; main() overrides per
# --selectivity-mode. SWEEPS.keys() is the canonical sweep-name set.
SWEEPS = {
    "fanout":      [100, 250, 500, 750, 1000],
    "selectivity": SELECTIVITY_POINTS[DEFAULT_SELECTIVITY_MODE],
}


def selectivity_points(selectivity_mode):
    """Param values for the selectivity sweep under the given mode (spec §5)."""
    return SELECTIVITY_POINTS[selectivity_mode]

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


def timed_call(session, url, payload, extract_timing=extract_server_timing):
    """Client-side perf_counter wrapping the full HTTP POST.

    Returns a dict (fair-timing spec §5):
        latency_ms, response_bytes,
        server_total_ms, ms_fetch, ms_build   (None if the server emits no
                                                parseable server_timing block)
    `latency_ms` = client_total. raise_for_status AND the JSON parse for the
    server-timing block both happen AFTER t1 — neither pollutes the client timer.
    """
    t0 = time.perf_counter()
    resp = session.post(url, json=payload)
    t1 = time.perf_counter()
    resp.raise_for_status()
    latency_ms = (t1 - t0) * 1000
    try:
        timing = extract_timing(resp.json())
    except ValueError:                       # malformed JSON body
        timing = None
    return {
        "latency_ms": latency_ms,
        "response_bytes": len(resp.content),
        "server_total_ms": timing["server_total_ms"] if timing else None,
        "ms_fetch": timing["ms_fetch"] if timing else None,
        "ms_build": timing["ms_build"] if timing else None,
    }


def _trial_row(backend_name, sweep_type, param_value, trial_num, result, timestamp,
               warmup, selectivity_mode=None):
    """Build one CSV row from a timed_fn result dict (fair-timing spec §4).

    network_ms = latency_ms − server_total_ms; negatives preserved (NOT clamped),
    blank when the server emitted no server_timing. selectivity_mode is None for
    non-selectivity sweeps (DictWriter writes it blank).
    """
    server_total = result["server_total_ms"]
    network_ms = (round(result["latency_ms"] - server_total, 3)
                  if server_total is not None else None)
    return {
        "backend": backend_name,
        "sweep_type": sweep_type,
        "selectivity_mode": selectivity_mode,
        "param_value": param_value,
        "trial_num": trial_num,
        "latency_ms": round(result["latency_ms"], 3),
        "server_total_ms": server_total,
        "ms_fetch": result["ms_fetch"],
        "ms_build": result["ms_build"],
        "network_ms": network_ms,
        "response_bytes": result["response_bytes"],
        "timestamp": timestamp,
        "warmup": warmup,
    }


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
    selectivity_mode=None,
):
    """Run warmup + timed trials for one sweep type, writing one CSV row per call.

    Args:
        backend_name:  string label written to every row (e.g. "jac")
        sweep_type:    "fanout" | "selectivity"
        param_values:  list of parameter values to sweep over
        timed_fn:      callable(param_value) -> result dict (timed_call's return:
                       latency_ms, response_bytes, server_total_ms, ms_fetch, ms_build)
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
            result = timed_fn(param_value)
            writer.writerow(
                _trial_row(backend_name, sweep_type, param_value, i, result,
                           timestamp_fn(), warmup=1, selectivity_mode=selectivity_mode))

        for i in range(trials):
            if clear_fn is not None:
                clear_fn()
            result = timed_fn(param_value)
            writer.writerow(
                _trial_row(backend_name, sweep_type, param_value, i, result,
                           timestamp_fn(), warmup=0, selectivity_mode=selectivity_mode))


# ── seed/verify control plane (untimed) ───────────────────────────────────────

def check_sweep_supported(sweep_type):
    """Guard hop_depth out of the implemented path (harness-fix-spec §4)."""
    if sweep_type in PHASE_4_5_SWEEPS:
        raise SystemExit(
            "hop_depth sweep is Phase 4.5 (load_extended_feed); not implemented. "
            "Remove it from --sweep.")


def load_point_spec(seed_dir, sweep_type, param_value, selectivity_mode=None):
    """Load one pre-generated neutral seed spec (reconciliation spec §6.2). Selectivity
    points are mode-namespaced (selectivity_{mode}_{param}.json)."""
    if sweep_type == "selectivity":
        fname = f"selectivity_{selectivity_mode}_{param_value}.json"
    else:
        fname = f"{sweep_type}_{param_value}.json"
    path = Path(seed_dir) / fname
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
    """Hard-fail post-seed verification (reconciliation spec §6.4). The like_count>K
    predicate is gone, so load_own_tweets returns ALL of the eval user's tweets:
    verify the full count (n_tweets) and the full key set, plus the H1 likes-cardinality
    guard. Channel-noise presence is verified by the adapter against the seed response."""
    tweets = backend.load_own_tweets()["tweets"]
    expected = spec["n_tweets"]
    if len(tweets) != expected:
        raise SystemExit(
            f"seed verify failed: expected {expected} own tweets, got {len(tweets)}")

    # The liker pool caps how many distinct likes a tweet can carry. The seed
    # generator guarantees len(likers) == min(like_count, pool_size); with no
    # pool in the spec, fall back to like_count (no cap).
    pool_size = len(spec.get("likers") or [])
    for t in tweets:
        expected_likes = min(t["like_count"], pool_size) if pool_size else t["like_count"]
        if len(t["likes"]) != expected_likes:
            raise SystemExit(
                f"seed verify failed: tweet like_count {t['like_count']} but got "
                f"{len(t['likes'])} likes (expected {expected_likes}) — thin/empty "
                f"likes payload (jac detached-liker risk, HARNESS_REVIEW H1)")

    got_keys = {_content_key(t["content"]) for t in tweets}
    expected_keys = {t["key"] for t in spec["tweets"]}
    if got_keys != expected_keys:
        missing = expected_keys - got_keys
        extra = got_keys - expected_keys
        raise SystemExit(
            f"seed verify failed: key set mismatch (missing={sorted(missing)[:5]}, "
            f"extra={sorted(str(e) for e in extra)[:5]})")


def verify_seeded_channels(seed_response, spec):
    """If the seed endpoint self-reports a channel count, assert it matches the spec
    (reconciliation spec §6.4 — don't run a timed point on a partially-seeded
    neighborhood). Tolerant: skip silently when the backend reports nothing."""
    if not isinstance(seed_response, dict):
        return
    reported = seed_response.get("seeded_channels")
    if reported is None:
        return
    if reported != spec["n_channels"]:
        raise SystemExit(
            f"seed verify failed: server seeded {reported} channels, "
            f"expected {spec['n_channels']}")


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
        "selectivity_mode": args.selectivity_mode,
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
    p.add_argument("--selectivity-mode", dest="selectivity_mode",
                   choices=list(SELECTIVITY_POINTS.keys()),
                   default=DEFAULT_SELECTIVITY_MODE,
                   help="Type-selectivity neighborhood mode (reconciliation spec §5): "
                        "fixed-target (default, n_tweets=1000 const) or fixed-total "
                        "(total=1000 const, reproduces the old fig6 points).")
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
            mode = args.selectivity_mode if sweep_type == "selectivity" else None
            param_values = (selectivity_points(mode) if sweep_type == "selectivity"
                            else SWEEPS[sweep_type])

            def setup_fn(param_value, _sweep=sweep_type, _mode=mode):
                spec = load_point_spec(args.seed_dir, _sweep, param_value, _mode)
                suffix = (f"selectivity_{_mode}_{param_value}"
                          if _sweep == "selectivity" else f"{_sweep}_{param_value}")
                username = f"bench_{args.run_id}_{suffix}"
                if args.skip_seed:
                    backend.auth(username, args.password)
                else:
                    backend.ensure_user(username, args.password)
                    guard_not_already_seeded(backend, username)
                    seed_resp = backend.seed(spec)
                    verify_seeded_channels(seed_resp, spec)
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
                selectivity_mode=mode,
            )
            label = f"{sweep_type} [{mode}]" if mode else sweep_type
            print(f"  {label}: done", flush=True)

    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_metadata(out_dir, args, started_at=started_at, finished_at=finished_at,
                       harness_git_sha=_git_sha(), cold_l1=bool(args.cold_l1))

    print(f"Results written to {out_path}")
    backend.session.close()


if __name__ == "__main__":
    main()
