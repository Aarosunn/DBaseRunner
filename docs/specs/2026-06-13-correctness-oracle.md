# Spec — Cross-System Correctness Oracle (Phase 5)

**Date:** 2026-06-13. **Status:** approved (brainstormed with Aaron), ready for TDD.
**Scope owner:** this session (design only). Implementing engineer: do not re-derive; where this
spec conflicts with code reality, stop and flag.
**Companions:** `seed-design-spec.md` (the deterministic seed this oracle treats as ground truth),
`harness-fix-spec.md` (timed-path rules, the existing `verify_seed` Tier-1 gate),
`2026-06-13-fair-timing-instrumentation.md` (the parse-after-timer seam whose normalized payload the
oracle reads).
**Design memory:** `content-oracle-design.md`, `seed-contract.md`, `figure-roster.md` (Phase 5 gates
every figure).

Every backend must return the **same data** for the same workload, or no latency comparison is
fair. Today only Tier-1 `verify_seed` exists (count / threshold / key-set / likes-cardinality). This
spec adds the **content oracle**: an absolute, fail-closed, multiset compare of each backend's
returned payload against the canonical seed projection, persisted as a verdict matrix that **gates
the plots**.

---

## 0. Scope

**In scope**

1. A pure **projector** that re-implements the workload predicate over the deterministic seed spec,
   yielding the canonical expected payload per sweep point (`oracle.py`).
2. A pure **comparator** (keyed symmetric multiset diff, fail-closed, capped diff) — also `oracle.py`,
   imported by the harness so the same code runs in-run and is unit-testable.
3. A **Tier-2 hook** in `harness.py`: after the existing Tier-1 `verify_seed`, deep-compare the
   already-fetched payload and **record** a per-point verdict (no abort). Writes
   `results/{backend}_correctness.json`.
4. A post-hoc **reconciler CLI** (`oracle.py main()`): merge the four per-backend sidecars into the
   `results/correctness.csv` verdict matrix + `results/correctness_report.json` (capped diffs + the
   all-fail-identically diagnostic).
5. A **fail-closed gate** in `plot.py`: plot only `status=="pass"` points; per-backend default with
   a `--strict-x` switch; visible mid-sweep-dropout annotation; missing `correctness.csv` is a hard
   error.

**Out of scope** — see §10. (Notably: H2 author-identity enforcement, the FP→type-selectivity
projector, multi-hop projections, and full-payload persistence.)

**Do not change:** the Tier-1 `verify_seed` semantics or its hard-abort behavior
(`harness.py:211-245`); `timed_call` timing semantics; the frozen timed-CSV schema
(`CSV_FIELDNAMES`); the warmup/trials flow in `run_sweep`. The oracle is **additive** — timing rows
are still written for points the oracle fails.

---

## 1. Locked model (decided 2026-06-13)

```
canonical                 = seed spec PROJECTED THROUGH THE WORKLOAD PREDICATE (re-implemented in Python)
got(backend, point)       = backend.load_own_tweets() → normalize_tweet (the shape already produced today)
verdict(backend, point)   = compare(canonical, got)   → pass | fail   (absolute, multiset, order-independent)
gate                      = plot only points whose verdict == pass     (fail-closed; missing == fail)
```

Five decisions, all locked:

1. **Source of truth = canonical seed projection, ABSOLUTE.** Not pairwise. Pairwise cannot catch a
   *consensus-wrong* answer — we have already hit exactly that (audit obs 280/284: the old seed
   created zero comments, every backend returned empty arrays and "agreed"). Absolute-vs-seed catches
   it; pairwise passes it. Cross-backend agreement is a **free corollary** of all-equal-canonical, so
   no pairwise pass is built or persisted.

2. **Project through the predicate; do not trust `expected_matching_keys`.** The projector
   independently applies `like_count > likes_threshold` over `spec["tweets"]`. This makes the oracle a
   true independent check (and turns `expected_matching_keys` into a cross-check — a mismatch there is
   a `seed_gen` bug, asserted loudly, §2.3).

3. **Two tiers, never folded together:**
   - **Tier 1 — seed integrity — HARD-ABORT** (existing `verify_seed`, unchanged). Data did not set up
     → nothing to benchmark → kill the run.
   - **Tier 2 — content oracle — RECORD + exclude, NO abort.** Data *is* set up but this backend
     returned wrong results. Timing rows are still written; the point is marked `fail` and excluded at
     plot time.

4. **Fail-CLOSED at the gate.** `plot.py` plots a point **iff** its verdict is exactly `pass`.
   `fail`, `error`, `missing`, and **no-verdict-at-all** are all excluded. Absence of a verdict reads
   as fail — that is what makes fail-closed real once Tier 2 stopped hard-aborting.

5. **Granularity = content + likes + comments multisets, order-independent.** Never counts (the
   load-bearing lesson). Backends return rows in different orders; compare as multisets.

**Free diagnostic (stolen from the rejected "both" option):** if **all** backends fail a point with
the **same** diff signature, log `suspect the canonical projection, not the DBs` — it distinguishes an
oracle-truth-model bug from a real backend disagreement (§6.3).

---

## 2. The canonical projection (`oracle.py`)

### 2.1 Projector registry (workload-keyed seam)

```python
# oracle.py
PROJECTORS = {
    "fanout":      project_load_own_tweets,
    "selectivity": project_load_own_tweets,   # current selectivity sweep IS load_own_tweets (FP path)
    # "type_selectivity": project_type_selectivity,   # reserved for refocus #9 (out of scope here)
    # "hop_depth":        project_extended_feed,       # reserved for Phase 4.5
}
```

The workload predicate lives in exactly one place per workload. `fanout` and `selectivity` share the
single-hop `load_own_tweets` projector today (both are "the eval user's own tweets matching
`like_count > threshold`"); the type-selectivity and multi-hop slots are reserved seams, not built.

### 2.2 `project_load_own_tweets(spec) -> dict[str, TweetElement]`

Pure function over one parsed seed spec (`seed/{sweep}_{param}.json`). Re-implements the workload:

1. `threshold = spec["likes_threshold"]`.
2. `matching = [t for t in spec["tweets"] if t["like_count"] > threshold]`  ← **predicate re-applied**.
3. For each matching tweet, build the canonical element (§3.1), keyed by its content token `t_NNNN`
   (`_content_key`, the same regex the harness already uses at `harness.py:197-199`).
4. Return `{key: element}`.

### 2.3 Self-cross-check (loud)

After projecting, assert `set(projected_keys) == set(spec["expected_matching_keys"])`. A mismatch
means the projector and `seed_gen` disagree on the predicate → raise `AssertionError` naming the
diff. This is a generator/oracle bug, not a backend bug; it must never be silently absorbed.

---

## 3. The comparator (`oracle.py`)

### 3.1 Canonical element keys (decided 2026-06-13)

Keyed by content token `t_NNNN`. Per-entity element tuples:

```
tweet   = (content, created_at_instant, tuple(sorted(likes)), tuple(sorted(comments)))
comment = (author, content, created_at_instant)
```

Per-field rules:

| field | source (seed) | source (got) | compare as | rationale |
|---|---|---|---|---|
| `content` | `t["content"]` | `normalize_tweet` `content` | exact string | strongest faithfulness check; catches IDs-only payloads |
| `created_at` (tweet) | `t["created_at"]` | normalized `created_at` | **parse-to-instant** (§3.2) | survives format drift; catches dropped/zeroed time (a real-bytes asymmetry) |
| `likes` | `t["likers"]` | normalized `likes` | **multiset** of usernames | identities, not just cardinality |
| `comments` | `t["comments"]` | normalized `comments` | **multiset** of `(author, content, instant)` | content + identity faithfulness |
| `author_username` | — (not in seed; identity is JWT/`self._username`) | normalized `author_username` | **excluded** from equality; separate dimension, **H2-gated** (§8) | jac hardcodes `"eval"`; including it guarantees a jac fail |
| `like_count` | `t["like_count"]` | normalized `like_count` | **not** in the Tier-2 element | covered by Tier-1 cardinality + implied by the likes multiset |
| `raw_id` | — | `normalize_tweet` `raw_id` | **ignored** | backend-native, divergent by design (audit obs 480) |

`likes` and `comments` use **multiset** semantics (`collections.Counter` over the canonicalized
sub-elements). Seed likers are unique per tweet, but multiset is used so a backend that duplicates a
like/comment is caught rather than set-collapsed.

### 3.2 Timestamp normalization contract (REQUIRED — or option-1 false-fails itself)

Backends emit the same instant as **different strings**: Neo4j returns the seed's `created_at`
verbatim (keeps `Z`); PG/SQLA parse and re-emit via `.isoformat()`, rewriting `Z → +00:00`. Exact
string compare false-fails a correct PG-vs-Neo4j pair. Contract:

```python
def parse_instant(s: str) -> datetime:
    """Parse an ISO-8601 string to a tz-aware UTC instant at SECOND resolution.
    Fail-closed: naive (tz-less) or unparseable input raises ValueError."""
    if not isinstance(s, str):
        raise ValueError(f"timestamp not a string: {s!r}")
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))   # accepts both Z and ±HH:MM
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime (no tzinfo): {s!r}")   # seed is all-aware; edge guard
    return dt.astimezone(timezone.utc).replace(microsecond=0)    # round to seed resolution (1s)
```

- **Seed resolution is whole seconds.** `seed_gen._BASE_TS = 2026-01-01T00:00:00Z`, tweet =
  `base + 60s·i`, comment = `tweet + 30s·(j+1)` — no sub-second component. Truncating `got` to seconds
  (`.replace(microsecond=0)`) means PG microseconds vs Neo4j string never sub-resolution false-fail,
  while a genuinely wrong second still fails.
- **Naive datetime → fail-closed.** The seed is all-tz-aware; a tz-less value from a backend is a
  shape failure, not a tolerance case.

### 3.3 Fail-closed shape handling

The comparator never silently passes an unrecognized shape (silent-pass is exactly how the old check
missed the IDs-only payload). A `got` payload **fails closed** when:

- a `got` tweet's `content` yields **no** `t_NNNN` token (`_content_key` returns `None`) → reason
  `unrecognized_shape:no_content_key` (this is the IDs-only-payload catch);
- `likes` or `comments` is not a list, or a comment is not a dict with `author`/`content` →
  `unrecognized_shape:bad_field`;
- `parse_instant` raises on any tweet/comment timestamp → `unrecognized_shape:bad_timestamp`;
- two `got` tweets collide on the same `t_NNNN` key → `unrecognized_shape:duplicate_key`.

### 3.4 `compare(expected, got) -> Verdict`

Both inputs are `dict[key, element]` (expected from §2.2; `got` built from `normalize_tweet`s under
the same element rules, with shape failures per §3.3).

```
missing  = expected keys not in got                          (capped, §3.5)
extra    = got keys not in expected                          (capped)
mismatch = keys in both whose element tuples differ          (capped; record which fields diverge)
status   = "pass" if (missing ∪ extra ∪ mismatch) == ∅ and no shape failure, else "fail"
status   = "error" if the comparator itself raised (bug in the oracle, not the backend)
```

`Verdict` fields: `status`, `reason` (short code, e.g. `ok`, `count_mismatch`, `likes_diff`,
`comments_diff`, `content_diff`, `timestamp_diff`, `unrecognized_shape:*`), `n_expected`, `n_got`,
and the capped `diff` (§3.5). `reason` is the **dominant** failure class for quick triage; the `diff`
carries the detail.

### 3.5 Capped symmetric diff (decided 2026-06-13)

Persist **why** it differs, not just **that** it differs — so we debug without re-running — but capped
so we never re-incur full-payload bloat.

```python
DIFF_CAP = 10   # max elements recorded per category
diff = {
  "missing":  [key, ...][:DIFF_CAP],                      # expected, not returned
  "extra":    [key, ...][:DIFF_CAP],                      # returned, not expected
  "mismatch": [{"key": key, "fields": ["likes", ...],     # which fields diverged
                "exp": {...capped...}, "got": {...capped...}}, ...][:DIFF_CAP],
  "capped":   {"missing": n_missing, "extra": n_extra, "mismatch": n_mismatch},  # true totals
}
```

For a `mismatch`, record only the diverging fields and, within a field (e.g. `likes`), the capped
symmetric set diff — not the whole liker list. `capped` carries the true totals so a reader knows the
list was truncated.

---

## 4. Tier-2 integration in `harness.py`

### 4.1 One fetch, two tiers

`setup_fn` (`harness.py:354-363`) currently calls `verify_seed`, which itself fetches
`backend.load_own_tweets()` and discards it. Refactor so the payload is fetched **once** and shared:

```python
def setup_fn(param_value, _sweep=sweep_type):
    spec = load_point_spec(args.seed_dir, _sweep, param_value)
    username = f"bench_{args.run_id}_{_sweep}_{param_value}"
    if args.skip_seed:
        backend.auth(username, args.password)
    else:
        backend.ensure_user(username, args.password)
        guard_not_already_seeded(backend, username)
        backend.seed(spec)
    payload = backend.load_own_tweets()["tweets"]     # single control-plane fetch
    verify_seed(backend, spec, tweets=payload)        # Tier 1 — HARD ABORT (unchanged checks)
    verdict = oracle.evaluate(spec, _sweep, payload)  # Tier 2 — RECORD, no abort
    verdicts.append({"sweep_type": _sweep, "param_value": param_value, **verdict})
```

- `verify_seed` gains an optional `tweets=` param (defaults to fetching itself, keeping its existing
  call sites and tests green). Its four checks and its `SystemExit` on failure are **unchanged**.
- `oracle.evaluate(spec, sweep_type, tweets)` = `compare(PROJECTORS[sweep_type](spec), build_got(tweets))`.
  It **returns** a verdict; it never raises for a backend mismatch (only for an internal oracle bug →
  `status="error"`). Trials then run normally regardless of the verdict.
- `verdicts` is a run-level list captured by the closure.

### 4.2 Sidecar write (survives a Tier-1 abort)

Accumulate verdicts and flush them in a `finally` around the sweep loop, so a later-point Tier-1
abort still persists the verdicts gathered before it (points after the abort have **no** verdict →
read as `missing` → fail-closed at the gate):

```python
try:
    for sweep_type in args.sweep:
        ...run_sweep(... setup_fn ...)
finally:
    write_correctness_sidecar(out_dir, args, verdicts)   # results/{backend}_correctness.json
```

### 4.3 Metadata note

`write_run_metadata` adds `"oracle_version": oracle.ORACLE_VERSION` so a figure can pin which oracle
produced its gate. (`harness.py:257-276`.)

---

## 5. Per-backend sidecar schema — `results/{backend}_correctness.json`

```json
{
  "run_id": "20260613...",
  "backend": "neo4j",
  "oracle_version": 1,
  "seed_spec_version": 2,
  "likes_threshold": 10,
  "points": [
    {"sweep_type": "fanout", "param_value": 100, "status": "pass",
     "reason": "ok", "n_expected": 25, "n_got": 25, "diff": null},
    {"sweep_type": "fanout", "param_value": 250, "status": "fail",
     "reason": "likes_diff", "n_expected": 63, "n_got": 63,
     "diff": {"missing": [], "extra": [],
              "mismatch": [{"key": "t_0042", "fields": ["likes"],
                            "exp": {"likes": ["liker_003", "liker_017"]},
                            "got": {"likes": []}}],
              "capped": {"missing": 0, "extra": 0, "mismatch": 63}}}
  ]
}
```

JSON (not CSV) at the per-backend layer because the capped symmetric diff is structured. The flat
verdict matrix that `plot.py` consumes is the **merged** `correctness.csv` (§6).

---

## 6. The reconciler — `oracle.py main()`

Runs **post-hoc**, after all backend runs for a `--run-id` have written their sidecars (matches the
`baselines.sh` sequential, teardown-between model — the four backends never coexist in one process).

```
python oracle.py --results results/        # default: every {backend}_correctness.json in results/
```

### 6.1 `results/correctness.csv` (the gate artifact `plot.py` reads)

Flat verdict matrix, one row per `(backend, sweep_type, param_value)`:

```
run_id, backend, sweep_type, param_value, status, reason, n_expected, n_got
```

`plot.py`'s `_SKIP = {"correctness.csv"}` (`plot.py:19`) already keeps this file out of the timed-CSV
read path; `plot.py` reads it separately as the gate (§7).

### 6.2 `results/correctness_report.json` (human/debug artifact)

The full capped diffs (from the sidecars) plus the diagnostic results (§6.3) plus a one-line summary
(`N points, P pass, F fail, M missing across B backends`).

### 6.3 All-fail-identically diagnostic

For each `(sweep_type, param_value)` present in the matrix:

- Collect every backend's verdict at that point.
- Compute a **diff signature** per failing backend = a stable hash of the *structure* of its symmetric
  diff (sorted `missing`/`extra` keys + sorted `mismatch` `(key, fields)` pairs + the `capped`
  totals) — structure, not full content.
- If **all** present backends `fail` **and** their signatures are **identical** →
  `report.diagnostics += "all-fail-identical at {sweep}={param}: suspect the canonical projection, not the DBs"`.
- If all fail but signatures **differ** →
  `"all-fail-divergent at {sweep}={param}: independent backend bugs, not the projection"`.

This is the cheap insurance from the rejected "both" option: it tells you whether a column-wide
failure is a truth-model bug (fix the projector) or genuine backend disagreement.

---

## 7. The gate — `plot.py`

### 7.1 Reader

```python
def read_correctness(results_dir):
    """Load results/correctness.csv → {(backend, sweep, param): status}.
    Missing file is a HARD ERROR — plotting is fail-closed and must not render
    ungated figures."""
```

- **Missing `correctness.csv` → `SystemExit`**: `"no correctness.csv in {dir} — run `python oracle.py`
  first; plotting is fail-closed and will not render ungated figures."` (Not silently empty — it tells
  the operator what to run.)
- A `(backend, sweep, param)` **absent** from the matrix → treated as status `missing` → excluded.

### 7.2 Fail-closed filter (default: per-backend)

In `aggregate`/`render`, a `(backend, sweep, param)` point is kept **iff**
`status[(backend, sweep, param)] == "pass"`. `fail` / `error` / `missing` / absent are excluded. A
failing backend simply gets a shorter curve; verified backends keep their points at that x (correct
under absolute-vs-seed — a passing backend deep-equals the canonical, hence is correct on its own and
equal to every other passing backend at that x).

### 7.3 `--strict-x` switch (camera-ready N-way)

`plot.py --strict-x`: keep a `(sweep, param)` point for **any** backend **iff every** backend present
in the matrix passed at that `param`. Same matrix, filtered two ways — the switch is one predicate
change, not a second pipeline. Use for headline figures that need a guaranteed N-way comparison at
every x.

### 7.4 Visible dropout annotation (REQUIRED in default mode)

When a backend has verified points for a prefix of a sweep then drops out, the figure must make the
absence **visible** so a short curve never reads as silent success:

- mark the backend's **last verified point** (e.g. an open marker / "×" terminator), and
- add a caption/legend note: `"{backend}: no verified data ≥ {first_failed_param}"`.

Pair with the correctness table (the rendered view of `correctness.csv`). If an **entire column**
all-fails (every backend excluded at one x), surface the §6.3 all-fail-identically diagnostic in the
caption — suspect the projection at that param, not the backends.

---

## 8. H2 dependency (named, as required)

`author_username` equality depends on **H2**: jac hardcodes the tweet `author_username = "eval"`
(`figure-roster`/future-roadmap #16), while baselines set the real eval username. Therefore:

- The §3.1 element key **excludes** `author_username` from tweet equality → the content oracle ships
  **now**, independent of H2.
- A separate **identity dimension** — "every returned tweet's `author_username` == the eval user's
  identity" — is **specified but OFF by default** (not part of the gate). A flag
  (`oracle.evaluate(..., check_identity=False)` default) enables it.
- **Enabling identity enforcement requires H2 first** (else jac fails every point on a known,
  unrelated gap). When H2 lands, flip the default and add `identity_diff` to the reason vocabulary.

This is the one cross-spec coupling; it is deliberately quarantined so the oracle is shippable before
H2.

---

## 9. Honesty labels / caption rules

1. **The oracle is a correctness GATE, not a figure.** Its output is a pass/fail matrix + a rendered
   table, never a latency curve (`figure-roster`: "Correctness = a Phase-5 gate+table, not a figure").
2. **A short curve is a correctness exclusion, not a measurement.** Default-mode dropouts must be
   annotated (§7.4) so a reader never reads "backend stops at x" as "backend got slow at x."
3. **`ms_build`/`ms_fetch` honesty labels are unaffected** — the oracle reads `normalize_tweet`
   payloads, not timing fields; it is orthogonal to the fair-timing descriptive/comparable split.
4. **Neo4j will fail likes/comments until its endpoints exist** (`harness-state-and-review`: no
   `like_tweet`/`add_comment`). That is correct fail-closed behavior — but note it will more often
   surface as a **Tier-1 hard-abort** (the likes-cardinality gate, H1) than a Tier-2 record, since a
   backend that never persists likes fails seed-integrity first.

---

## 10. Out of scope (do NOT build this pass)

- **H2 author-identity enforcement** — the identity dimension is specified + flagged off (§8); flipping
  it on is H2's job.
- **FP→type-selectivity projector** (`project_type_selectivity`) — reserved seam only (#9 refocus).
- **Multi-hop projector** (`project_extended_feed`) — Phase 4.5 seam only.
- **Full-payload persistence** — rejected: gitignored, scp-ferried bloat, low value since the seed is
  deterministic and re-derivable (a single-backend re-run is cheap). Sidecars carry only verdicts +
  capped diffs.
- **Pairwise cross-backend compare** — rejected (§1.1); a corollary of absolute-vs-seed.
- **`plot.py` correctness-table rendering polish** — the gate + a minimal table are in scope; figure
  styling of the table is a later pass.

---

## 11. TDD test plan (ordered — red → green per item)

**Phase A — projector (`oracle.py`, `tests/test_oracle.py`):**
1. `project_load_own_tweets` returns exactly the `like_count > threshold` subset, keyed by `t_NNNN`.
2. Projected key set equals `spec["expected_matching_keys"]`; a doctored spec where they disagree
   raises `AssertionError` (§2.3).
3. Element tuple shape matches §3.1 (content, instant, sorted likes, sorted comments).

**Phase B — timestamp contract (`tests/test_oracle.py`):**
4. `parse_instant` maps `...Z` and `...+00:00` to the **same** instant (the PG-vs-Neo4j case).
5. Sub-second drift truncates to the seed second (no false-fail).
6. Naive (tz-less) string → `ValueError` (fail-closed).

**Phase C — comparator (`tests/test_oracle.py`):**
7. Identical payloads (reordered) → `pass` (order-independence).
8. Empty `likes` on one tweet → `fail`, `reason="likes_diff"`, mismatch names `t_NNNN` + field
   `likes` (the Neo4j case).
9. Empty `comments` array everywhere → `fail` (the consensus-wrong regression: this is the obs-280/284
   bug the oracle must catch).
10. IDs-only content (no `t_NNNN`) → `fail`, `reason="unrecognized_shape:no_content_key"`.
11. Duplicate `t_NNNN` in `got` → `unrecognized_shape:duplicate_key`.
12. Missing / extra tweet keys → captured in `diff.missing` / `diff.extra`.
13. Diff capping: 50 mismatches → `diff.mismatch` length ≤ `DIFF_CAP`, `capped.mismatch == 50`.
14. `author_username` divergence alone → **`pass`** (excluded from the element key, §8); with
    `check_identity=True` → `fail`, `reason="identity_diff"`.

**Phase D — harness Tier-2 hook (`tests/test_harness_main.py`):**
15. `verify_seed` accepts an injected `tweets=` and does not re-fetch (existing call sites/tests green).
16. A Tier-2 `fail` does **not** abort: trials still run, the verdict is appended, `{backend}.csv`
    rows are still written.
17. A Tier-1 failure still hard-aborts, and the `finally` flush persists the partial verdict list.
18. Sidecar JSON schema (§5): one entry per attempted point, with `status`/`reason`/`n_*`/`diff`.

**Phase E — reconciler (`tests/test_oracle.py`):**
19. Merge four sidecars → `correctness.csv` rows = Σ points across backends, correct columns.
20. All-fail-identical signatures → diagnostic string emitted; all-fail-divergent → the other string.
21. A backend whose sidecar is absent contributes **no** `pass` rows (→ `missing` at the gate).

**Phase F — plot gate (`tests/test_plot.py`):**
22. Missing `correctness.csv` → `SystemExit` (fail-closed, names the fix).
23. Default mode: a backend with `fail` at `param=p` is absent from the curve at `p` but present at
    its `pass` params; verified backends keep `p`.
24. `--strict-x`: one backend failing at `p` drops `p` for **all** backends.
25. A point absent from the matrix is excluded (absence == fail).
26. Dropout annotation present when a backend ends early (assert the legend/caption note string).

**Regression:** existing `plot.py`, `harness.py`, `seed_gen.py` test suites stay green; schema-touching
asserts updated under TDD.

**Final:** `cavecrew-reviewer` on the completed diff.

---

## 12. Open questions / engineer verifications

1. **Does each backend's `seed_tweets` actually persist likers + comments?** If Neo4j's `seed_tweets`
   ignores them, Tier-1 (likes-cardinality, H1) hard-aborts Neo4j before Tier 2 ever runs — the
   oracle's Neo4j story is then "blocked at Tier 1 until the endpoints exist," not "fails at Tier 2."
   Confirm on the first live run; the behavior is correct either way, but the operator-visible failure
   differs.
2. **`datetime.fromisoformat` on the target Python.** `Z`-handling is native in 3.11+; the
   `.replace("Z", "+00:00")` shim in §3.2 makes it version-robust — confirm the harness runtime and
   keep the shim regardless.
3. **Comment author key coverage.** `normalize_comment` already folds
   `author`/`username`/`author_handle`/`handle` → `author` (`base.py:15-21`); confirm no backend emits
   a comment-author key outside that set before trusting the comments multiset.
4. **`oracle.evaluate` import direction.** The harness imports `oracle` (as it imports `seed_gen`);
   `oracle` must not import `harness` (no cycle). Keep projector + comparator dependency-free
   (stdlib + the seed spec only).
