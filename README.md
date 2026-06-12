# DBaseRunner — JacDB benchmark harness

Unified harness for Figures 5/6: a sweep point is a **dataset**, not a request
parameter. For each `(sweep_type, param_value)` the harness seeds a fresh eval
user with exactly that point's deterministic data, verifies the seed, then times
an empty `POST /walker/load_own_tweets` under that user's auth. Backends: jac
(jac-cloud), postgres, sqlalchemy, neo4j. See `docs/specs/`.

## Setup (uv)

Harness dependencies are managed by [uv](https://docs.astral.sh/uv/). The servers
under `servers/` deploy separately (k8s) and carry their own deps — they are not
installed here.

```bash
uv sync                 # create/refresh .venv from pyproject.toml + uv.lock
uv run pytest           # run the test suite
```

## Generate seed data

Deterministic; committed under `seed/`. Regenerate with:

```bash
uv run python seed_gen.py --out seed/
```

## Run a benchmark

```bash
uv run python harness.py --backend jac --url http://localhost:8000 \
    --run-id myrun --sweep fanout selectivity
```

Phase 5 requires the **same `--run-id` across all four backend runs**. Cache is
all-warm by default; `--cold-l1` is an opt-in jac-only diagnostic. `--sweep
hop_depth` is reserved for Phase 4.5 and exits with a message.
