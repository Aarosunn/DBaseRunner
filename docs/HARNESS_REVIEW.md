# Harness Adversarial Review — Confirmed Defects

**Date: 2026-06-13.** Independent adversarial read of the `DBaseRunner/` harness
(client + 4 servers + k8s + seed/plot glue), hunting for undocumented bugs and
fairness/logic issues *beyond* the gaps already tracked in `HARNESS_CONTEXT.md`
and `HANDOFF.md`. Every item below was re-verified against source a second time;
unconfirmed or refuted claims are listed at the end so they are not re-raised.

**Method:** cross-checked each adapter's parse path against its server's response
shape; each server's predicate boundary against the seed generator; CSV schema
against plot columns; env-var wiring against the `jaseci` runtime source; pod
specs across all four manifests.

**Scope note:** "Confirmed" means verified in committed source. A few items are
*confirmed in code* but their runtime *manifestation* needs a live cluster to
observe (flagged inline). The headline timed path is
`POST /walker/load_own_tweets`; several defects sit on the ablation/diagnostic or
deploy paths, not the headline — scope is called out per item.

---

## Adversarial triage — what actually must be fixed (2026-06-13, post-cleanup)

Second-pass bad-mood verdict on necessity, after re-verifying every item against
live source. The findings below are evidence; this is the priority call. Two state
changes since the first pass: **`k8s/jac/` was deleted** and jac was pulled from
`orchestrate.sh` (resolves M4, re-scopes C1/C3); jac now deploys only via
`servers/jac/deploy.sh` (`--scale`).

**🔴 MUST FIX before any trustworthy 4-way run — these invalidate the comparison:**
- **C1** resources — pg + neo4j uncapped (BestEffort), sqla 1 CPU. Latency not
  comparable. ⚠ jac is now `--scale`-deployed → its pod sizing is jac-cloud's, not
  a manifest's; equalizing all four needs the jac pod inspected/pinned too.
- **C2** `postgres:15` vs `postgres:16-alpine` — confounds the single-hop-vs-SQLA
  "same engine" comparison (the most defensible win). Pin one image.
- **M3** `postgres-app:latest` — the stale-image footgun the README warns about.
  Bump to an immutable tag (`:v1`).
- **H1** `verify_seed` blind to the `likes` list — cheap hard-gate that turns the
  §13 detached-liker unknown into a caught failure instead of a silent thin payload.

**🟠 MUST FIX before showing figures to anyone (not before running):**
- **H3** plot.py hardcodes `"smoke run / 2 trials"` onto every figure — will print
  on a real 30-trial render. Derive from `_meta.json`.

**🟡 SHOULD FIX for clean figures (quality, not validity):**
- **L2** seed floor — fanout=100 @ 5% = 5 matching tweets (noise). Raise fixed
  selectivity or the fanout floor.

**🔵 VERIFY (gap exposed by deleting `k8s/jac/`):**
- `JAC_TOPOLOGY_INDEX` was only set in the deleted manifest (`:103`). It now rides
  solely on `jac.toml topology_index=true` (the fallback). `deploy.sh` sets no env.
  Confirm on-cluster that the `--scale` pod runs with the index actually ON.

**⚪ DEFER — real, but belongs to a later phase, do NOT do now:**
- **H2** author=`"eval"` → Phase 5 content oracle · **M1** `ms_build` inconsistency
  → §5 fair-timing wiring · **M2** `clear_cache` L2 close → `--cold-l1` only ·
  **C3-survivor** `JAC_INDEX_ENABLED` no-op → GTI-ablation line · **L1/L3/L4** →
  hardening.

**✅ RESOLVED:** **M4** — `k8s/jac/` deleted + jac removed from `orchestrate.sh`
(2026-06-13). The ConfigMap-wipe race can no longer occur.

> **Coordination:** the jac `:pub` isolation fix is owned by a separate session
> editing `servers/jac/server.jac`. The MUST/SHOULD items above touch
> `k8s/*.yaml`, `harness.py`, `plot.py`, `seed_gen.py` — **none touch `server.jac`**,
> so they can proceed in parallel without collision.

---

## 🔴 Critical — invalidate cross-backend latency as currently deployed

### C1. Pod CPU/memory caps are unequal, and two backends are uncapped
- jac: `requests 500m / limits 2000m` — `k8s/jac/deployment.yaml:118-124`
- sqlalchemy app: `requests 250m / limits 1000m` — `k8s/sqlalchemy/deployment.yaml:90-96`
- **postgres: no `resources:` block at all** — `k8s/postgres/deployment.yaml` (whole file; app container `:54-62`)
- **neo4j: no `resources:` block at all** — `k8s/neo4j/deployment.yaml` (whole file; app container `:59-71`)

PG and neo4j run BestEffort QoS (burst to the whole node); sqla is throttled to
1 CPU; jac to 2. Latency is not comparable across backends until every app pod —
and its DB pod — has identical requests *and* limits. `HARNESS_CONTEXT.md §10`
lists "equalize pod CPU/mem" as a TODO; the actual state (two pods with zero
limits, a 2× split between the two that have them) is worse than "not yet done"
and is unflagged.

**Fix:** add one identical `resources:` block (requests == limits → Guaranteed
QoS, no bursting) to all six manifest-managed pods — pg/neo4j/sqla × (app + DB).
**Done 2026-06-13:** all six pinned to `cpu 1000m / mem 1Gi`.
⚠ **jac is NOT manifest-managed** (`--scale`-deployed → jac-cloud sets its own pod
sizing). The jac app pod's `resources` must be inspected on-cluster
(`kubectl get pod -l app=jaseci -o jsonpath=...`) and pinned to the same
`1000m/1Gi` before the 4-way comparison is trustworthy.

### C2. The two Postgres-backed backends run different Postgres majors
- hand-tuned PG store: `image: postgres:15` — `k8s/postgres/deployment.yaml:17`
- sqlalchemy store: `image: postgres:16-alpine` — `k8s/sqlalchemy/deployment.yaml:65`,
  and the sqla app connects to it via `DATABASE_URL=...@dbaserunner-postgres:5432` (`:184`)

SQLAlchemy-vs-PG is the "same storage engine, measure only app-layer overhead"
comparison (`HARNESS_CONTEXT.md §11`). It is confounded by PG 15 (debian) vs PG
16 (alpine — different libc/malloc), plus different DB names. Pin both to one
identical image.

---

## 🟠 High — silent data/payload divergence that `verify_seed` does not catch

### H1. `verify_seed` only checks the scalar `like_count`; it is blind to the likes list
- `verify_seed` asserts `len(tweets)==expected`, every `like_count > threshold`,
  and the content-key set — but never inspects `likes` — `harness.py:179-203`
- jac seeds the liker **Profiles detached** (no root, no grant) and stores
  `like_count` as an independent scalar field — `servers/jac/server.jac:976, 984`
- `load_own_tweets` rebuilds `likes` by edge traversal `[tweet<-:Like:<-Profile]`
  — `servers/jac/server.jac:900`

**Confirmed in code:** the verify step cannot detect a jac response whose
`likes` array is empty while `like_count` says 11–20. **Needs cluster to observe:**
whether the detached liker Profiles actually fail to resolve under `--scale`
(the persistence caveat in `HARNESS_CONTEXT.md §13`). If they do, jac ships a
thinner payload than PG/SQLA/neo4j (which return full liker arrays), which
biases the `response_bytes` parity guard downward for jac and will break the
Phase-5 content oracle — and verify will have passed anyway.

**Fix:** have `verify_seed` also assert `len(t["likes"]) == t["like_count"]` for
matching tweets on every backend. That converts the §13 caveat into a hard gate.

### H2. Every jac tweet carries `author_username = "eval"`
- the auto-created Profile hardcodes `username="eval"` — `servers/jac/server.jac:965`
- tweets copy `author_username = my_profile.username` — `servers/jac/server.jac:982`
- baselines set it to the real bench username (`u.username`) — e.g.
  `servers/postgres/src/routes/walker.py:498`

The bench flow never calls `setup_profile`, so the name is never corrected.
`verify_seed` checks content keys + counts, not author, so this passes. It is a
cross-backend payload-field divergence that matters for Phase-5 correctness and
for `response_bytes` parity.

**Fix:** create the Profile with the JWT user's identity, or have the adapter
set the profile name during seeding.

### H3. plot.py bakes a false "smoke run" caption onto every figure
- `_CAPTION` is hardcoded `"... 2 timed trials per point (smoke run) ..."` for
  both sweeps — `plot.py:41-49`

Render the real 30-trial headline and the PNG still reads "2 timed trials
(smoke run)." Misleading provenance printed on the artifact.

**Fix:** derive trial count + cache mode from the `_meta.json` sidecar (or pass
them in) instead of a literal string.

---

## 🟡 Medium

### M1. Server per-phase `ms_build_payload` is inconsistent across backends — and currently unused
- jac: build folded into the traversal loop; the separate span measures nothing
  ≈ 0 — `servers/jac/server.jac:913-914`
- postgres: hardcoded `"ms_build_payload": 0.0` — `servers/postgres/src/routes/walker.py:527`
- sqlalchemy: real separate build span — `servers/sqlalchemy/src/routes/walker.py:326-328`
- neo4j: real separate build span — `servers/neo4j/src/main.py:166-182`
- the harness CSV records only `latency_ms` + `response_bytes`; none of the
  server `ms_*` fields are read — `harness.py:34-43`

So the "fair per-phase timing" design (`HARNESS_CONTEXT.md §5`) is not wired into
the harness at all, and when the Phase-Breakdown figure is built off these
fields, two backends will show ~0 build and two real → a fabricated asymmetry.
`§5` flags jac's vestigial span but not that PG shares it and sqla/neo4j diverge
the other way.

**Fix:** when wiring §5, split fetch vs build on jac+PG to match sqla/neo4j, and
define one identical phase decomposition across all four.

### M2. `clear_cache` closes the L2 connection and never reopens it
- `l2.close()` with no reopen — `servers/jac/server.jac:943` (same pattern in the
  `bench_pushdown` clear path, `:785`)

In `--cold-l1` mode `clear_cache` runs before *every* trial, so after trial 1 the
Redis L2 is closed for the remainder of the run; the diagnostic then measures a
broken-cache path, not a cold one. Scope: `--cold-l1` is an opt-in jac-only
diagnostic, not the headline path.

**Fix:** stub/flush L2 without closing the connection, or reopen it after close.

### M3. postgres-app ships on the `:latest` tag — contradicts the repo's own minikube warning
- `image: dbaserunner/postgres-app:latest` + `imagePullPolicy: Never` —
  `k8s/postgres/deployment.yaml:56-57`

`README.md §3.1` says in bold "Never reuse a tag" because minikube serves the
stale local layer for a reused tag. neo4j does it right (`:v1`,
`k8s/neo4j/deployment.yaml:61`). postgres silently risks running old code after
a rebuild.

**Fix:** bump postgres to an immutable tag (`:v1`) like neo4j.

### M4. ✅ RESOLVED (2026-06-13) — The `k8s/jac` ConfigMap manifest can wipe the source populated by `k8s-configmap.sh`
> **Fixed:** `k8s/jac/` was deleted, jac removed from `orchestrate.sh`'s loop (now
> errors with a pointer to `deploy.sh`), and the jac case dropped from
> `k8s-configmap.sh`. The race below can no longer occur. Original finding kept for
> the record.
- `k8s-configmap.sh` imperatively creates `dbaserunner-jac-src` **with data** via
  `kubectl create ... | kubectl apply` — `k8s-configmap.sh:29-32`
- `k8s/jac/deployment.yaml` also declares a `dbaserunner-jac-src` ConfigMap **with
  no `data:`** (placeholder, commented out) — `k8s/jac/deployment.yaml:27-41`
- `orchestrate.sh` runs the configmap script, then `kubectl apply -f k8s/jac/`
  (which includes the empty ConfigMap) — `orchestrate.sh:84-90`

Applying the no-data ConfigMap after the populated one resets the
last-applied-config to "no data" → kubectl's 3-way merge strips the populated
keys → empty `/src` → init container copies nothing. Scope: only triggers on the
deprecated in-pod manifest path; `README.md §3.1` already says deploy jac
natively via `servers/jac/deploy.sh`, but `orchestrate.sh` still drives this path
and lists jac **first** in `ORDER` (`orchestrate.sh:30`).

**Fix:** delete the placeholder ConfigMap object from `k8s/jac/deployment.yaml`
(let `k8s-configmap.sh` be the sole owner), or remove jac from `orchestrate.sh`
entirely and document the native-only path.

---

## ⚪ Low / hardening

### L1. The predicate threshold `10` is duplicated in five places with no single source
- `seed_gen.LIKES_THRESHOLD = 10` — `seed_gen.py:18`
- hardcoded `> 10` in all four servers — `servers/postgres/src/routes/walker.py:516`,
  `servers/sqlalchemy/src/routes/walker.py:316`, `servers/neo4j/src/main.py:155`,
  `servers/jac/server.jac:899`

Bump the constant and the servers silently desync; `verify_seed` then fails with
a confusing count mismatch. (Currently all five agree on 10, so figures are
correct today.) Consider sourcing the literal from the seed spec / config.

### L2. The seed grid undershoots the documented min-target floor
- fanout sweep fixes selectivity at 5% — `seed_gen.py:23` —
  so fanout=100 yields `(100*5+50)//100 = 5` matching tweets (`seed_gen.py:47`)

`HARNESS_CONTEXT.md §8` wants ≥ ~20–30 target nodes at the lowest point; 5 (and
12 at fanout=250) is below that. Low-fanout points are noisy and the
`response_bytes` curve starts near-degenerate. Raise the fixed selectivity or
the floor of the fanout grid.

### L3. A single non-2xx mid-sweep aborts the entire run
- `timed_call` calls `resp.raise_for_status()` with no retry/skip — `harness.py:77`;
  it propagates out through `run_sweep`/`main`, and `orchestrate.sh` runs under
  `set -e` (`orchestrate.sh:19`)

One transient 500 during a 30-trial × 10-point × 4-backend run loses everything.
Acceptable for a controlled benchmark but brittle; consider a bounded retry on
the timed call.

### L4. sqla `src/routes/` is shipped without an `__init__.py`
- the ConfigMap maps only `user.py` + `walker.py` under `src/routes/` —
  `k8s-configmap.sh:38-41` — while `src/__init__.py` IS shipped, making `src` a
  regular package and `src.routes` an implicit namespace subpackage

Mixing a regular package with a namespace subpackage is fragile. It ran live on
2026-06-12, so Python's namespace-package fallback evidently covers it; flagging
as a latent footgun, not an active break.

---

## Corrected from the first pass — do NOT re-raise

### ✅ REFUTED: "the GTI enable env var is wrong in the deployment"
The first pass claimed the deploy set the wrong var and the index might be OFF.
**Refuted.** The runtime gate is `JAC_TOPOLOGY_INDEX` (verified:
`jaseci/jac/jaclang/runtimelib/impl/topo_utils.impl.jac:3-9`, "the
`JAC_TOPOLOGY_INDEX` env var … when set, is authoritative; the config flag is the
fallback"). The deployment sets `JAC_TOPOLOGY_INDEX="true"`
(`k8s/jac/deployment.yaml:103`) and `jac.toml` sets `topology_index = true`
(`servers/jac/jac.toml:16`). The harness meta + HANDOFF use the correct name.
**The headline jac timing path runs with the index correctly enabled.**

### ⚠️ The narrow defect that survives (C3, rescoped): the bench ablation toggle is a no-op
`JAC_INDEX_ENABLED` — toggled by the three `bench_*` walkers
(`servers/jac/server.jac:610, 696, 768` and surrounding) and named as the
"GTI ON/OFF" knob in `HARNESS_CONTEXT.md §1/§5/§10` — appears **nowhere** in the
`jaseci` runtime (grep: zero hits outside DBaseRunner). So the index ON/OFF
ablation those walkers implement does nothing: the `index_enabled` field is
reported but the index state never changes, and the paper's `on_l1≈5` vs
`off_l1≈103` signature will NOT separate when toggled. Scope: the
ablation/`naive-jac` reference line (§9) and the optional B5 validation
diagnostic — **not** the headline figures.

**Fix:** the bench walkers and the §1/§5/§10 doc guidance must toggle
`JAC_TOPOLOGY_INDEX`, not `JAC_INDEX_ENABLED`.

---

## Checked and cleared (looked suspect, verified correct)

These were probed adversarially and are sound — recorded so they are not
re-investigated:

- **Auth token parse paths all match their servers.** jac `data.token`
  (`backends/jac.py:17` ↔ `user.py`-equivalent), pg/sqla `data.token`
  (`user.py:48,56` / `routes/user.py:43,56`), neo4j `data.result.token`
  (`backends/neo4j.py:13` ↔ `main.py:54,84`). All correct.
- **`load_own_tweets` envelope vs adapter extraction.** Baselines return
  `data.result = [tweets]` and base reads `data.result` (`base.py:103-107`); jac
  returns tweets in `data.reports[0].tweets` and the jac adapter overrides to read
  exactly that (`backends/jac.py:19-27`). Consistent.
- **Predicate boundary is exact on all four.** matching tweets seed `like_count`
  11–20, non-matching 0–10 (`seed_gen.py:112`); every server uses strict `> 10`,
  so the boundary value 10 is correctly excluded everywhere.
- **Selectivity math is sound.** round-half-up `(n*pct+50)//100`
  (`seed_gen.py:45-47`) and `rng.sample` of exactly that many matching indices
  (`seed_gen.py:104`) means the server predicate returns exactly
  `expected_matching` rows; verify's count + key-set checks are tight.
- **Per-point user isolation on the baselines.** each point seeds a distinct
  `bench_<run>_<sweep>_<param>` user; `load_own_tweets` filters by `author_id` /
  bearer username, so no teardown is needed and points don't cross-contaminate.
- **Comment author-key normalization.** neo4j stores comments as JSON with key
  `author`; PG/SQLA return `username`; `normalize_comment` accepts both
  (`base.py:15-21`).
