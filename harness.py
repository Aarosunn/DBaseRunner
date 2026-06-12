"""Unified benchmark harness for JacDB.

Methodology decisions (all locked in):
  TIMING:   Client-side perf_counter wrapping full HTTP POST — no server fields.
            t0 before session.post(); t1 after it returns (full body received).
            raise_for_status() outside the timer (pure Python, no I/O).
  WARMUP:   20 requests per config point. Warmup rows written (warmup=1).
            plot.py filters warmup=0 only. No clear_cache during warmup.
  CACHE:    Jac only — POST /walker/clear_cache before each timed trial.
            Clears L1 (in-process graph), L2 Redis stays warm (l2.close() is
            disconnect-only; verify on running clarity build when SSH available).
            Baselines: no clear. Buffer pools accumulate warmth naturally.
            Asymmetry documented: Jac L1-cold/L2-warm vs baseline always-warm.
  SESSION:  One persistent requests.Session per backend run. TCP pooling is
            part of each system's production profile.
  CSV:      One row per trial, warmup and timed alike. No pre-aggregation.
            Aggregation (median, p25/p75) happens in plot.py only.
"""

import argparse
import csv
import sys
import time
from pathlib import Path

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

SWEEPS = {
    "fanout":      [100, 250, 500, 750, 1000],
    "selectivity": [10, 25, 50, 75, 100],
    "hop_depth":   [1, 2, 3],
}

TRIALS = 30
WARMUP_COUNT = 20


def timed_call(session, url, payload):
    """Client-side perf_counter wrapping full HTTP POST.

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
):
    """Run warmup + timed trials for one sweep type, writing one CSV row per call.

    Args:
        backend_name:  string label written to every row (e.g. "jac")
        sweep_type:    "fanout" | "selectivity" | "hop_depth"
        param_values:  list of parameter values to sweep over
        timed_fn:      callable(param_value) -> (latency_ms, response_bytes)
        writer:        csv.DictWriter — must already have headers written
        warmup_count:  requests fired before timed trials (warmup=1 rows)
        trials:        timed requests per param_value (warmup=0 rows)
        clear_fn:      callable() -> None, called before each timed trial.
                       Jac only (clears L1, L2 stays warm). None for baselines.
        timestamp_fn:  injectable for tests; defaults to time.time
    """
    if timestamp_fn is None:
        timestamp_fn = time.time

    for param_value in param_values:
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


def _build_parser():
    p = argparse.ArgumentParser(description="JacDB benchmark harness")
    p.add_argument("--backend", required=True,
                   choices=["jac", "postgres", "sqlalchemy", "neo4j"],
                   help="Backend to benchmark")
    p.add_argument("--url", required=True, help="Base URL of the backend service")
    p.add_argument("--user", default="bench@example.com", help="Auth username")
    p.add_argument("--password", default="benchpass", help="Auth password")
    p.add_argument("--user-id", default="bench_user", dest="user_id",
                   help="jac_id of the user to query")
    p.add_argument("--sweep", nargs="+",
                   choices=list(SWEEPS.keys()),
                   default=list(SWEEPS.keys()),
                   help="Sweep types to run (default: all)")
    p.add_argument("--trials", type=int, default=TRIALS)
    p.add_argument("--warmup", type=int, default=WARMUP_COUNT)
    p.add_argument("--out", default="results",
                   help="Directory to write CSVs (default: results/)")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)

    from backends import JacBackend, PostgresBackend, SQLAlchemyBackend, Neo4jBackend
    import requests

    backend_classes = {
        "jac":        JacBackend,
        "postgres":   PostgresBackend,
        "sqlalchemy": SQLAlchemyBackend,
        "neo4j":      Neo4jBackend,
    }
    backend = backend_classes[args.backend](args.url)
    backend.auth(args.user, args.password)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.backend}.csv"

    session = requests.Session()
    load_url = f"{args.url.rstrip('/')}/walker/load_own_tweets"

    clear_fn = None
    if args.backend == "jac":
        clear_url = f"{args.url.rstrip('/')}/walker/clear_cache"
        def clear_fn():
            resp = session.post(clear_url, json={})
            resp.raise_for_status()

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()

        for sweep_type in args.sweep:
            param_values = SWEEPS[sweep_type]

            def timed_fn(param_value, _sweep=sweep_type):
                if _sweep == "fanout":
                    payload = {"user_id": args.user_id, "limit": param_value, "selectivity": 100}
                elif _sweep == "selectivity":
                    payload = {"user_id": args.user_id, "limit": 1000, "selectivity": param_value}
                else:  # hop_depth
                    payload = {"user_id": args.user_id, "hop_depth": param_value}
                return timed_call(session, load_url, payload)

            run_sweep(
                args.backend, sweep_type, param_values,
                timed_fn, writer,
                warmup_count=args.warmup,
                trials=args.trials,
                clear_fn=clear_fn,
            )
            print(f"  {sweep_type}: done", flush=True)

    print(f"Results written to {out_path}")
    session.close()


if __name__ == "__main__":
    main()
