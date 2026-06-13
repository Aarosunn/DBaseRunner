# Harness Fix Spec — Phase 3 → Phase 4 readiness

**Status:** spec, ready to implement.
**Author:** Session B (design only). Implementing engineer: do not re-derive; where this spec
conflicts with code reality, stop and flag.
**Companion:** `seed-design-spec.md` (same directory) — defines the seed generator, the neutral
spec format, and the per-server endpoint requirements this spec depends on.

**Provisional-grounding warning.** This design takes `audit/SOURCE_OF_TRUTH.md` §1.3
(post-audit corrections) as given. A parallel fact-check session may revise §1.3. Items in this
spec that depend on it are tagged `[CONTINGENT-§1.3]`. Everything else stands regardless.

---

## 0. Scope

Fixes four verified gaps in `DBaseRunner/harness.py` + `DBaseRunner/backends/`, and wires the
seed step into `main()`:

1. **Sweep is a data no-op** — `load_own_tweets` takes no params on any server
   (jac `server.jac:880`, postgres `walker.py:482`, sqlalchemy `walker.py:302`, neo4j
   `main.py:149`); `main()` sends `limit`/`selectivity` that every server ignores
   (`harness.py:177-184`). Every sweep point currently measures identical data.
2. **Adapters bypassed** — `main()` calls `timed_call()` directly (`harness.py:184`); no code
   path ever invokes `backend.load_own_tweets()`.
3. **`base.py` contract encodes the wrong model** — `load_own_tweets(user_id, limit,
   selectivity)` (`base.py:21`) describes a parameterized followee traversal that no server
   implements.
4. **`hop_depth` in default sweep** — `SWEEPS` includes `hop_depth` (`harness.py:40`) and the
   default `--sweep` runs it (`harness.py:127`), but Phase 4.5 (`load_extended_feed`) is not
   built; default runs emit mislabeled rows.

Out of scope: the seed generator itself and all server-side endpoint changes (see
`seed-design-spec.md`); Phase 4.5 implementation; Phase 5 comparison logic (seams only).

**Do not change:** `timed_call()` timing semantics (perf_counter wraps only `session.post`,
`raise_for_status` outside the timer, `harness.py:47-57`); CSV column set and order
(`harness.py:26-35`); warmup/trials defaults (20/30); jac served via jac-cloud
`POST /walker/<name>` — that is the paper's claim under test, not a defect.

---

## 1. Fix 1 — Realize the sweep through data (re-seed per param point)

### 1.1 Principle

A sweep point is a **dataset**, not a request parameter. The harness must, for each
`(sweep_type, param_value)`:

1. create a fresh eval user dedicated to that point,
2. seed exactly the data the point prescribes (from a pre-generated spec file),
3. verify the seed landed,
4. then run warmup + timed trials against `POST /walker/load_own_tweets` with an **empty
   payload** under that user's auth.

The timed request is identical at every point on every backend; only the data behind the
authenticated user differs. `param_value` in the CSV records the seeded parameter.

### 1.2 Eval-user namespacing (replaces reset-between-points)

Jac has **no data-delete endpoint** — `clear_cache` (`server.jac:917`) clears L1/L2 caches
only; nothing in the jac server deletes nodes/edges. Rather than demand a destructive walker on
one backend only, use the same non-destructive mechanism everywhere:

- Eval username per point: `bench_{run_id}_{sweep_type}_{param_value}`
  (e.g. `bench_r1_fanout_250`). Password: `args.password`.
- `run_id` is a new CLI flag (`--run-id`, default: harness start time formatted
  `%Y%m%d%H%M%S`). **Phase 5 requires the same `--run-id` across all four backend runs** so
  the seeded usernames/data match; document this in `--help` text.
- Single-hop `load_own_tweets` reads only the authenticated user's own tweets on every backend
  (jac: caller's root → Profile → Tweet; PG/SQLA: `WHERE author_id = current_user`; neo4j:
  `MATCH (p {jac_id: $uid})-[:POST]->(t)`), so stale data under *other* users cannot enter the
  result set. Accumulated dead data per full run is ~7,600 tweets per backend (see seed spec
  §4) — negligible for index-backed per-author lookups, and symmetric across backends.
- `backend.reset()` (new, §3) wipes the store where a `clear_data` endpoint exists
  (PG `walker.py` via `db.reset()`, SQLA `__init__.py:135`, neo4j `main.py:190`). Jac's
  `reset()` is a logged no-op. Call it once at run start only when `--reset` is passed;
  default off, because wiping on three backends but not jac is itself an asymmetry.
  Namespacing, not reset, is the correctness mechanism.

### 1.3 Double-seed guard

If a `run_id` is accidentally reused, `seed()` would duplicate the dataset. Before seeding:
call `backend.load_own_tweets()`; if it returns a non-empty tweet list, hard-fail:

```
SystemExit: eval user bench_r1_fanout_250 already has data; pass a fresh --run-id,
or --skip-seed to reuse the existing dataset.
```

`--skip-seed` (new flag) skips `ensure_user`-then-`seed` and goes straight to verify + timing —
for re-running timing against an already-seeded run.

### 1.4 Post-seed verification (hard fail)

After seeding, call `backend.load_own_tweets()` (the adapter path — normalized shape, §3) and
assert, against the point's spec file:

- `len(tweets) == spec["expected_matching"]`,
- every returned tweet has `like_count > LIKES_THRESHOLD` (the predicate constant, seed spec §2),
- the set of content-embedded keys (`t_0042` prefixes, seed spec §6) equals the spec's expected
  matching-key set.

Any mismatch → `SystemExit` with the diff. This catches: seed endpoint silently dropping
fields, jac `:pub`-walker root-binding failures (§6 open question 3), predicate not implemented
server-side, and double-seeding.

### 1.5 `run_sweep` change: per-point setup hook

Current `run_sweep` (`harness.py:60-115`) iterates `param_values` internally, so per-point
seeding needs a hook. Add one keyword param:

```python
def run_sweep(backend_name, sweep_type, param_values, timed_fn, writer,
              warmup_count=20, trials=30, clear_fn=None, timestamp_fn=None,
              setup_fn=None):   # NEW: called as setup_fn(param_value) once per
                                # param point, before that point's warmup block
```

`setup_fn=None` keeps all 28 existing tests green. `main()` passes a closure that performs
§1.2–§1.4 (ensure user → seed → verify) for the point. Setup time is **never** written to the
CSV; it is untimed control-plane work.

### 1.6 New `main()` flow (normative pseudocode)

```python
backend = backend_classes[args.backend](args.url)
if not backend.health():
    raise SystemExit(f"{args.backend} health check failed at {args.url}")
if args.reset:
    backend.reset()

load_url = f"{args.url.rstrip('/')}/walker/load_own_tweets"

for sweep_type in args.sweep:
    if sweep_type == "hop_depth":
        raise SystemExit("hop_depth sweep is Phase 4.5; not implemented. "
                         "Remove it from --sweep.")        # see §4
    param_values = SWEEPS[sweep_type]

    def setup_fn(param_value, _sweep=sweep_type):
        spec = load_point_spec(args.seed_dir, _sweep, param_value)   # seed spec §3
        username = f"bench_{args.run_id}_{_sweep}_{param_value}"
        backend.ensure_user(username, args.password)                  # §3
        if not args.skip_seed:
            guard_not_already_seeded(backend)                         # §1.3
            backend.seed(spec)                                        # §3
        verify_seed(backend, spec)                                    # §1.4

    def timed_fn(param_value):
        return timed_call(backend._session, load_url, {})             # §2: empty payload

    run_sweep(args.backend, sweep_type, param_values, timed_fn, writer,
              warmup_count=args.warmup, trials=args.trials,
              clear_fn=clear_fn, setup_fn=setup_fn)                   # clear_fn: see §5

write_run_metadata(out_dir, args, backend)                            # §1.7
```

Note `backend.auth(args.user, args.password)` at startup (`harness.py:151`) is **replaced** by
per-point `ensure_user()`; the session token changes at each param point. `--user`/`--user-id`
flags: delete `--user-id` (nothing consumes it any more); keep `--user`/`--password` as the
registration password source and for any backend whose register flow needs an email-like field.

New CLI flags summary: `--run-id` (str, default = start timestamp), `--seed-dir`
(str, default `seed/`), `--skip-seed` (store_true), `--reset` (store_true).

### 1.7 Run metadata sidecar

The timed CSV schema is frozen. Write `results/{backend}_meta.json` alongside it:

```json
{
  "run_id": "...", "backend": "...", "harness_git_sha": "...",
  "seed_spec_version": 1, "likes_threshold": 10,
  "warmup": 20, "trials": 30, "sweeps": ["fanout", "selectivity"],
  "started_at": "...", "finished_at": "...",
  "notes": {"jac_topology_index": "from k8s env JAC_TOPOLOGY_INDEX, record actual value"}
}
```

Phase 5 joins backends on `run_id`; the defense narrative needs `likes_threshold` and spec
version pinned to each figure.

---

## 2. Fix 2 — Timing path decision: time the raw POST; adapters are control plane

**Decision: keep `timed_call(session, url, payload)` as the only code inside the timer.**
Adapters never appear in the timed path.

Rationale:
- The rebuild's TIMING decision (HARNESS_REBUILD.html, Key Decisions) is "client-side
  `perf_counter` wrapping HTTP POST — same for every backend." One shared `session.post` is
  the strongest form of "same code path"; adapter methods would put per-backend Python
  (jac's `_normalize`, future per-backend quirks) inside or adjacent to the timer.
- Response parsing/normalization cost belongs to the *consumer* of the data (Phase 5), not the
  latency measurement. `response_bytes = len(resp.content)` already captures payload-size
  effects without parsing.

Consequences:
- **Timed payload becomes `{}` for every backend and every sweep type.** The current
  `user_id`/`limit`/`selectivity` payload keys (`harness.py:177-184`) are dead weight the
  servers ignore — delete the whole per-sweep payload branch. Identity comes from the
  `Authorization` header on `backend._session` (jac: JWT → root; others: bearer-username).
  This also kills the cosmetic asymmetry of jac receiving unused params (audit obs 479).
- **Adapters own everything untimed:** `ensure_user`/`auth`, `health`, `seed`, `reset`, the
  seed-verify fetch, and the Phase-5 correctness fetch — all via `backend.load_own_tweets()`
  with its normalized return shape. jac's `_normalize` (`backends/jac.py:30`) stops being dead
  code: the verify step (§1.4) exercises it every param point, on every run.
- `main()` keeps reading `backend._session` (`harness.py:160`). Promote it to a public
  attribute or property `backend.session` while touching the file; underscore access from the
  harness is the only awkwardness this leaves.
- Endpoint seam for Phase 4.5: a module-level map keeps the future multi-hop endpoint out of
  per-sweep `if`s:

  ```python
  SWEEP_ENDPOINTS = {"fanout": "load_own_tweets",
                     "selectivity": "load_own_tweets",
                     "hop_depth": "load_extended_feed"}   # Phase 4.5, guarded in main()
  ```

---

## 3. Fix 3 — Corrected `backends/base.py` contract

Replace the current interface (`base.py:6-37`) with:

```python
class BackendBase(ABC):
    """One instance per benchmark run. Owns one authenticated requests.Session,
    exposed as .session, used for ALL traffic to this backend (timed and untimed)."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    @abstractmethod
    def ensure_user(self, username: str, password: str) -> None:
        """Register username if it does not exist, then log in.
        On success self.session carries the Authorization header.
        Register-then-409/conflict followed by successful login is the
        normal idempotent path, not an error."""

    @abstractmethod
    def auth(self, username: str, password: str) -> None:
        """Log in an existing user (no registration). Used by --skip-seed runs
        and Phase-5 tooling."""

    @abstractmethod
    def load_own_tweets(self) -> dict:
        """UNTIMED control-plane fetch of the authenticated user's own tweets.
        POST /walker/load_own_tweets with empty JSON body.

        Returns the normalized shape (identical across backends):
            {"tweets": [
                {"content": str,          # begins with seed key token, e.g. "[t_0042] ..."
                 "author_username": str,
                 "created_at": str,       # ISO-8601 UTC
                 "like_count": int,
                 "likes": list[str],      # liker usernames
                 "comments": list[{"author": str, "content": str, "created_at": str}]},
                ...]}
        Backend-native tweet IDs MUST NOT be required by callers (ID types diverge
        across backends — audit obs 480); adapters MAY include them under "raw_id".
        Used for: post-seed verification, Phase-5 cross-backend comparison.
        NEVER called inside the timed path."""

    @abstractmethod
    def health(self) -> bool:
        """True if the backend answers its health endpoint.
        jac: POST /walker/health; others: GET /health."""

    @abstractmethod
    def seed(self, spec: dict) -> None:
        """Load one param-point spec (parsed dict, schema in seed-design-spec.md §3)
        into the store via the server's import endpoints, as/for the currently
        authenticated eval user. Idempotency is NOT required here; the harness
        guards against double-seeding (harness-fix-spec §1.3)."""

    @abstractmethod
    def reset(self) -> None:
        """Best-effort full data wipe. PG/SQLA/neo4j: POST /walker/clear_data.
        Jac: no server support — log a warning and return (do NOT raise)."""

    def clear_cache(self) -> None:
        """Backend cache clear between trials. Default: no-op.
        JacBackend overrides with POST /walker/clear_cache.
        Only invoked in cold-L1 diagnostic mode (harness-fix-spec §5)."""
        return None
```

Changes from current code, per adapter:

| Adapter | Changes |
|---|---|
| all four | drop `(user_id, limit, selectivity)` from `load_own_tweets`; send `{}`; add `ensure_user` (POST `/user/register`, tolerate already-exists, then login), `reset`, `seed` (per seed spec §8); rename `_session` → `session` |
| `jac.py` | keep `_normalize` but re-target it to the normalized shape above (map walker report keys, hoist `like_count`); keep POST `/walker/health`; `reset()` = warn + return; override `clear_cache()` with the closure currently inlined at `harness.py:163-168` |
| `postgres.py` / `sqlalchemy.py` | normalize their `{"data": {"reports": [...]}}` envelope to the shape above (today they return raw JSON, `postgres.py:22`); map `author_handle`/`handle` comment keys → `author` |
| `neo4j.py` | same normalization; parse `comments` (stored as JSON strings, `main.py:113-124`) into dicts |

`register` body differences (jac wants `{email/username, password}` JWT flow; others
username/password returning token=username) are absorbed inside each adapter's
`ensure_user` — `main()` never branches on backend.

---

## 4. Fix 4 — `hop_depth` out of the default sweep, seam stubbed

- `SWEEPS` (`harness.py:37-41`): **remove** the `"hop_depth": [1, 2, 3]` entry. Define it in a
  separate constant next to it: `PHASE_4_5_SWEEPS = {"hop_depth": [1, 2, 3]}` with a comment
  pointing at HARNESS_REBUILD Phase 4.5 (`load_extended_feed`, all backends).
- Default `--sweep` therefore becomes `["fanout", "selectivity"]` automatically
  (`harness.py:127` derives it from `SWEEPS.keys()` — no further change).
- `--sweep hop_depth` (explicit) → `SystemExit` with the Phase 4.5 message (§1.6). Parser
  `choices` should accept it so the error is ours, not argparse's.
- Delete the `hop_depth` payload branch (`harness.py:182`) — superseded by the empty payload
  (§2). The future seam is `SWEEP_ENDPOINTS` (§2) plus a `hop_depth` entry in the seed-spec
  format (seed spec §10: requires a follows-graph, deferred).
- `plot.py:11` mentions `fig7_hop_depth.png` — leave; it's a comment about a future figure.

---

## 5. Flagged for decision — per-trial `clear_cache` contradicts the rebuild plan `[CONTINGENT-§1.3]`

Current harness: jac-only `clear_fn` fires **before every timed trial**
(`harness.py:163-168` + `run_sweep` trial loop). Two documents disagree with it:

- HARNESS_REBUILD.html, Key Decisions / CACHE: *"All warm. Run 20 warmup requests per config
  before timing. No `clear_cache` between trials. Redis L2 is Jac's architecture — measure it
  as designed."*
- SOURCE_OF_TRUTH §1.3(a): `clear_cache` clears L1 but only **disconnects** L2
  (`l2.close()`, not a flush) → per-trial clearing reproduces precisely the original audit's
  L1-cold/L2-warm jac vs fully-warm baselines asymmetry — the asymmetry the rebuild exists to
  remove.

**Recommendation:** default run takes the HARNESS_REBUILD decision — `clear_fn=None` for all
backends including jac; all-warm, 20-request warmup does the conditioning. Keep the capability
behind a new `--cold-l1` flag (jac-only effect, via `backend.clear_cache()`, §3) for an
explicitly-labeled diagnostic sweep; when set, record `"cold_l1": true` in the metadata sidecar
so the rows can never be mistaken for the headline figures.

Contingency: if the parallel audit finds `l2.close()` actually flushes Redis, per-trial
clearing would mean fully-cold jac trials — even further from "all warm," same recommendation.
If session-1 owners have already documented per-trial clearing as intentional (memory obs 451
says "intentional and documented"), that documented rationale must be reconciled with the
HARNESS_REBUILD CACHE decision **before** any headline run; this spec does not silently pick
the winner — it defaults to the written rebuild plan.

---

## 6. Open questions / engineer verifications

1. **Fanout-axis wording conflict.** HARNESS_REBUILD.html defines Fig 5 fan-out as
   100–1000 *followees*; the seed contract (and the single-hop query reality) defines it as
   *tweets authored by the eval user* (own `POST` edges). The seed contract wins — it is the
   only definition consistent with a no-param `load_own_tweets`. HARNESS_REBUILD.html and
   `base.py` docstrings carry the stale "followees" wording; flag for the doc owner
   (do not edit HARNESS_REBUILD.html from this work stream).
2. **Postgres has no `GET /health`** (route inventory of `servers/postgres/src/routes/` shows
   none), yet `backends/postgres.py:30` calls it. Server-side requirement recorded in seed
   spec §8; harness work merely depends on it.
3. **jac `:pub` walker + Bearer token root binding.** `load_own_tweets` is `walker:pub`
   (`server.jac:880`). The design requires that calling it WITH a JWT executes against that
   user's root. Verify on the running build; if `:pub` ignores the token and binds the public
   root, change the walker to auth-required (it is always called authenticated here). The
   §1.4 verify step will catch this within the first param point either way.
4. **`backend.session` reuse across `ensure_user` calls.** Confirm replacing the
   `Authorization` header on a live `requests.Session` cleanly re-authenticates against
   jac-cloud (no server-side session affinity). Expected yes (stateless JWT), verify once.
5. **Trials/warmup at higher fanouts.** 20 warmup + 30 trials at fanout=1000/selectivity=100%
   moves ~50 × ~0.5 MB responses (seed spec §5); fine on cluster-internal links — confirm no
   client-side bottleneck on clarity before headline runs.

## 7. Test plan additions (TDD, extend `tests/test_harness.py`)

- `setup_fn` hook: called once per param point, before that point's first warmup row; not
  called when `None`; exceptions propagate (no CSV rows written for the failed point).
- Empty timed payload: `timed_fn` posts `{}` — assert via mocked session for each sweep type.
- `hop_depth` guard: `--sweep hop_depth` exits with the Phase 4.5 message; default parser
  sweep list == `["fanout", "selectivity"]`.
- `verify_seed`: passes on exact match; fails on count mismatch, on a sub-threshold
  `like_count`, on key-set mismatch (three separate tests).
- Double-seed guard: non-empty pre-seed fetch → SystemExit naming the eval user.
- `ensure_user`: register-409-then-login path succeeds (mocked adapter-level test — first
  adapter tests in the suite; current 28 tests never touch adapters).
- Metadata sidecar: file written, contains `run_id`, `likes_threshold`, `cold_l1` flag state.
