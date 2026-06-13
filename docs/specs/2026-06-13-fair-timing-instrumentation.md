# Spec — Fair-Timing Server Instrumentation

**Date:** 2026-06-13. **Status:** approved, ready for TDD.
**Scope owner:** this session (harness + servers + `HARNESS_CONTEXT.md §5`).
**Companion:** `HARNESS_CONTEXT.md §5` (methodology), `harness-fix-spec.md` (timed-path rules).

Adds the fair-timing design's server-side decomposition to the current client-only
timing. Single-hop `load_own_tweets` only. This is **instrumentation** — produce + record
the timing data; figure rendering is a later pass.

---

## 1. What we measure (locked model)

```
ms_fetch + ms_build           = the two server sub-phases (descriptive)
server_total                  = handler entry → return (measured independently)
client_total (= latency_ms)   = perf_counter around the POST (unchanged)
network_ms = latency_ms − server_total   (derived, NOT measured)
```

- **`server_total` is THE comparable cross-backend number** (substrate, network-excluded).
  It carries the claims.
- **`ms_fetch` / `ms_build` are per-backend DESCRIPTIVE** — they explain *where a backend's
  own server_total goes*. They are **NOT cross-comparable** (see §2 honesty-labels).
- **`client_total`** = delivered latency (provisional under port-forward).
- **`network_ms`** = derived context, not a claim-bearer.

Phases reduced from the design's literal four (`auth/query/fetch/build`) to **two measured
sub-phases + residual**. `ms_auth` and `ms_query` are dropped (auth is pre-handler framework;
query-plan is not separable from fetch on PG and is trivial for indexed single-hop).

---

## 2. Honesty-labels (REQUIRED — bake into spec, code comments, and §5 caption rules)

These are labeling rules, not redesigns. A reader/figure must not be able to misread the data.

1. **`server_total` is the only comparable number; fetch/build split is per-backend
   descriptive.** Caption rule for the eventual phase-breakdown figure: **PG `ms_build = 0`
   means "built in-SQL, inside `ms_fetch`," NOT "PG builds for free."** A reader must not rank
   `ms_build` (or `ms_fetch`) segments across backends — only `server_total` is rankable.

2. **Consequences of 4-phases → 2:**
   - Phase-breakdown decomposes into **`ms_fetch` / `ms_build` / residual / network**, NOT the
     old named `auth/query/fetch/build`. (`residual = server_total − ms_fetch − ms_build` =
     in-handler glue + any in-handler auth resolve.)
   - **Auth is excluded from `server_total`** (FastAPI `Depends` / jac-cloud run pre-handler)
     → **`network_ms` = transport + framework + AUTH.** Label it exactly that. **Never call
     `network_ms` "network latency."**
   - **Per-backend auth cost is now invisible** (folded into provisional `network_ms`). If an
     auth claim ever matters, the path forward is to restore a measured `ms_auth` — noted, not
     built.

3. **`network_ms`:** allow negatives (do **not** clamp), mark provisional, report its
   **distribution** not a point median. `server_total` carries claims; `network_ms` is context.

4. **Invariant:** `server_total ≥ ms_fetch + ms_build` (sub-spans nest inside the entry→return
   span → holds by construction; the test catches a backend whose independent `server_total`
   undershoots its own parts).

---

## 3. Server handler contract

Every `load_own_tweets` handler adds, inside its existing report dict, a uniform block:

```json
"server_timing": {"ms_fetch": <float>, "ms_build": <float>, "server_total": <float>}
```

- Envelope location: `data.reports[0].server_timing` (pg/sqla/neo4j), `reports[0].server_timing`
  (jac — jac-cloud omits the `data` wrapper). The extractor (§5) handles both.
- This **replaces** the current ad-hoc `ms_traversal` / `ms_build_payload` fields.
- `server_total` = `perf_counter()` at the **first line of the handler body** → just before the
  response object is built. `ms_fetch` / `ms_build` are sub-spans inside it.

### Per-backend marker table (also goes in HARNESS_CONTEXT §5)

| backend | `ms_fetch` = | `ms_build` = | `server_total` = | note |
|---|---|---|---|---|
| postgres | `json_agg` SQL round-trip (`cur.execute`+`fetchone`) | `0.0` | entry → return | **built in-SQL, inside `ms_fetch`** — comment in code |
| sqlalchemy | query exec (`db.execute(...).all()`) | `.report()` hydration loop | entry → return | the ORM-hydration tax |
| neo4j | cypher run + `.data()` | list-comprehension build | entry → return | `_bearer_username` (in-handler auth) lands in residual |
| jac | materialize `[my_profile-->(?:Tweet, like_count > 10)]` into a list | dict-append loop | entry → return | **query semantics UNCHANGED** (GTI+FP intact) — measurement seam only |

- **jac caveat:** the seam splits "resolve the typed-neighbor list" from "build dicts" — it must
  NOT change the comprehension (typed `?:Tweet` → GTI, literal `like_count > 10` → FP). Do **not**
  touch the `walker:pub` declaration (that is §13, a separate fix). Marker logic is
  compile-validatable (`jac validate` via MCP) even though live jac is blocked on §13.
- **PG `ms_build = 0.0`** is honest here (the C engine builds the JSON); code comment required.

---

## 4. CSV schema delta

`harness.CSV_FIELDNAMES` becomes (new columns inserted after `latency_ms`):

```
backend, sweep_type, param_value, trial_num,
latency_ms, server_total_ms, ms_fetch, ms_build, network_ms,
response_bytes, timestamp, warmup
```

- `latency_ms` = `client_total` (meaning unchanged).
- `network_ms = round(latency_ms − server_total_ms, 3)` when `server_total_ms` present, else blank.
  **Negatives allowed (no clamp).**
- New marker columns blank when the server emitted no parseable `server_timing` (see §5
  best-effort rule).
- Warmup rows carry the new columns too (uniform schema; aggregation stays in `plot.py`).
- No back-compat shim; old result CSVs are regenerated.

---

## 5. Harness capture path

**Return type change:** `timed_call` returns a **dict** (was a 2-tuple). `run_sweep`'s `timed_fn`
contract follows. This ripples through existing `test_harness.py` (tuple-unpacking +
`MagicMock(return_value=(10.0, 100))`) — those tests are updated to the new contract under TDD.

```python
# backends/base.py — one shared extractor (mirrors normalize_tweet); best-effort, NOT fail-closed
def extract_server_timing(body: dict) -> dict | None:
    reports = (body.get("data") or {}).get("reports") or body.get("reports") or []
    if not reports or not isinstance(reports[0], dict):
        return None
    st = reports[0].get("server_timing")
    if not isinstance(st, dict):
        return None
    try:
        return {"server_total_ms": float(st["server_total"]),
                "ms_fetch": float(st["ms_fetch"]),
                "ms_build": float(st["ms_build"])}
    except (KeyError, TypeError, ValueError):
        return None
```

```python
# harness.py
def timed_call(session, url, payload, extract_timing=extract_server_timing):
    t0 = time.perf_counter()
    resp = session.post(url, json=payload)
    t1 = time.perf_counter()
    resp.raise_for_status()                       # outside timer (unchanged)
    latency_ms = (t1 - t0) * 1000
    try:
        timing = extract_timing(resp.json())      # AFTER t1 — never pollutes client_total
    except ValueError:                            # bad JSON
        timing = None
    return {
        "latency_ms": latency_ms,
        "response_bytes": len(resp.content),
        "server_total_ms": timing["server_total_ms"] if timing else None,
        "ms_fetch": timing["ms_fetch"] if timing else None,
        "ms_build": timing["ms_build"] if timing else None,
    }
```

`run_sweep`: consumes the dict, computes `network_ms`, writes all columns for warmup + timed rows:

```python
r = timed_fn(param_value)
server_total = r["server_total_ms"]
network_ms = round(r["latency_ms"] - server_total, 3) if server_total is not None else None
writer.writerow({... "latency_ms": round(r["latency_ms"], 3),
                 "server_total_ms": server_total, "ms_fetch": r["ms_fetch"],
                 "ms_build": r["ms_build"], "network_ms": network_ms,
                 "response_bytes": r["response_bytes"], ...})
```

**Best-effort rule:** missing/unparseable `server_timing` → blank marker columns + a once-per-run
warning. A run never dies over a timing field (timing is measurement; the Phase-5 content oracle
is the fail-closed one).

---

## 6. Out of scope (deferred — do NOT build this pass)

- **`plot.py` rendering** of the phase-breakdown bar + p50/p95/p99 bands. `csv.DictReader` ignores
  unknown columns → existing fanout/selectivity plots keep working untouched. One test asserts
  `plot.py` still runs on the new schema.
- **In-cluster bench client** (kill port-forward) — only cleans up `client_total`/`network_ms`,
  which carry no claim. Provisional-caption mitigation covers the gap. (Lighter path when built:
  configmap-mounted `python:3.12` pod or `kubectl run` curling the ClusterIP — avoids a registry
  round-trip.)
- **jac `:pub` → root-isolation fix** (§13) — separate, gates live jac runs but not this code.
- **`ms_auth` restoration** — only if an auth claim ever matters.
- **figure-roster.md / memory files** — owned by the SOT session; do not edit here.

---

## 7. TDD test list (ordered — red → green per item)

**Phase A — extractor (`backends/base.py`, `tests/test_backends.py`):**
1. `extract_server_timing` pulls `{server_total_ms, ms_fetch, ms_build}` from `data.reports[0].server_timing` (baseline envelope).
2. …from `reports[0].server_timing` (jac envelope, no `data` wrapper).
3. Returns `None` on: no reports, `reports[0]` not a dict, no `server_timing`, `server_timing` missing a key, non-numeric value, empty body.
4. Coerces numeric strings/ints to float.

**Phase B — harness capture (`harness.py`, `tests/test_harness.py`):**
5. `timed_call` returns a dict with all five keys; `latency_ms ≥ 0`, `response_bytes` correct.
6. `timed_call` populates `server_total_ms/ms_fetch/ms_build` from a mock resp whose `.json()` carries `server_timing`.
7. `timed_call` → marker keys `None` when `.json()` has no `server_timing` (and when `.json()` raises `ValueError`).
8. `raise_for_status` still called, still outside the timer; HTTP error still propagates; URL+payload unchanged.
9. Client timer excludes the JSON parse (inject a slow `extract_timing`; assert `latency_ms` unaffected).
10. Update `CSV_FIELDNAMES` (new columns present, correct order).
11. `run_sweep` writes new columns on warmup + timed rows; `network_ms = latency_ms − server_total_ms`.
12. `run_sweep`: `server_total_ms None` → `network_ms` blank, no crash.
13. `run_sweep`: **negative `network_ms` is preserved, not clamped** (server_total > latency_ms case).
14. CSV round-trip (DictWriter→DictReader) carries the new columns.
15. Update existing tuple-based tests (`(latency, bytes)` → dict) — they go green on the new contract.

**Phase C — per-server contract (each `servers/<b>/tests/`):**
16. `load_own_tweets` response has `server_timing` with numeric `ms_fetch`, `ms_build`, `server_total`, all `≥ 0`.
17. **Invariant:** `server_total ≥ ms_fetch + ms_build` (allow tiny float epsilon).
18. PG: `ms_build == 0.0`.
19. sqla / neo4j / jac: `ms_build` present (`≥ 0`).
20. jac: file compiles (`jac validate`) and the report shape includes `server_timing`.

**Phase D — plot tolerance (`tests/test_plot.py`):**
21. `plot.py` runs without error on a CSV containing the new columns (ignores extras, plots `latency_ms`).

**Regression:** existing 111 harness + 26 server tests stay green (schema-touching asserts updated).

**Final:** `cavecrew-reviewer` on the completed diff.
