# Harness Context & Design Decisions

**Last updated: 2026-06-13.** Single source of truth for the *current* methodology and the corrections we made this session. Read this before changing the harness so we don't re-introduce confusions the paper report and the first rebuild baked in. Companion: `HARNESS_REBUILD.html` (phase plan).

---

## 0. The big corrections (read first — these caused real confusion)

1. **Selectivity = TYPE selectivity, NOT filter pushdown.** The "selectivity sweep" fetches the X% of a node's mixed-type neighbors that are the target type (e.g., 5% Tweets among channels/comments/friends). It is **not** a field-predicate test (`like_count > k`). The current harness mislabels it — `servers/jac/server.jac` `load_own_tweets` uses `[my_profile-->(?:Tweet, like_count > 10)]`, which is the *filter-pushdown* path. **This must be refocused** (see §4). Filter pushdown is a separate, later test the class hadn't reached.
2. **Two distinct Jac mechanisms — never conflate them:**
   - **GTI (type)** — resolves *which* typed neighbors exist, O(1), in-memory, from the topology index. This is what the selectivity/fanout figures exercise.
   - **Filter pushdown (field predicate)** — pushes `field > literal` into storage. A *separate* future test.
3. **"Jac returns IDs only" was half the story.** In the old `bench_feed`, the DB traversal *was* timed (the query ran); the loop just appended IDs. The real asymmetry was that baselines additionally fetched/built engagement data (likes/comments) and resolved the user *inside* the timer, while Jac didn't. Don't say "Jac skipped the DB query."
4. **`db-replicate/` is a flawed first attempt — ignore it.** Not the researchers' harness, not this one. Being purged. Its degenerate-selectivity bug (`TWEETS_POOL=10`) is *its* flaw, not the paper's.
5. **Is filter pushdown merged into jaclang `main`? UNSURE.** The GTI/FP *logic* was read from the `filter_pushdown` branch (`FETCH_HEAD`), not `main`. The topology files exist on the fork's `main`, but the full FP path / env-var wiring wasn't confirmed merged. PyPI 0.13.5 ships the topology source but never reads `JAC_INDEX_ENABLED` → the figure ON/OFF toggle is a no-op on PyPI; it works on the branch. **Verify before relying on any specific build.**

---

## 1. How Jac GTI + Filter Pushdown actually work (verified from `filter_pushdown` source)

**GTI = a write-time, type-bucketed in-memory index (the SAM).** Structure: `sam[node_id]["n:Tweet"] = (in_ids, out_ids)`.
- Built at **edge-creation** (`on_edge_created → add_edge`), with **MRO fan-out** (a `PostNode(BaseContent)` is filed under both `n:PostNode` and `n:BaseContent`). Also `e:<EdgeType>` columns.
- `query_targets(src, node_type)` = `sam[src]["n:Tweet"]` — **two O(1) dict hits, returns the typed-neighbor IDs, never scans non-matching edges.** Multi-hop = `resolve_chain`, one O(1) lookup per node per hop.
- `query_targets` returns **IDs only**; node bodies are fetched in step 2.

**The two-step query pipeline** (wired at `runtime.impl.jac:1340-1353`):
1. **Topology** — `plan_query → resolve_chain → query_targets` → set of typed-neighbor UUIDs (in-memory, O(1)).
2. **Storage fetch + predicate pushdown** — `extract_query_filter` parses the comprehension's AST → `QueryFilter`; `filtered_bulk_get` builds **one** Mongo `find({_id:{$in:ids}, props.<field>:{$op:v}})`. Non-matching docs are **never deserialized**. (Scalar fields are copied to `props.<name>` at write via `_extract_props`.)

**🚨 The literal-RHS gotcha (load-bearing):** `extract_query_filter` only extracts a predicate when the RHS is a **literal** (`Int/Float/...`). A variable/walker-field RHS (`like_count > self.k`) is **silently skipped** → no pushdown → loads everything and filters in Python. So **never parameterize a pushed-down threshold; vary the seed data at a fixed literal.** (`filtered_bulk_get` also silently falls back to unfiltered on any exception.)

**Storage hierarchy (under `jac start --scale`):** L1 in-process dict + L2 Redis pod + L3 Mongo pod.
- **L1 holds live Python anchor objects** → an L1 hit is a local lookup with **zero network hop, zero deserialization**. This is Jac's structural latency edge (PG/Neo4j/SQLA always pay an app→DB-pod hop even warm).
- L2 hit = Redis hop + deserialize. L3 = hop + query + deserialize.
- Index is **per-root** (built only for persistent nodes under a root) → the root-isolation bug (§13) corrupts the index, not just the data.

---

## 2. What Jac actually is in the stack

Jac is **not a database engine.** It's an **OSP runtime + topology index over MongoDB**:

```
           PG-handtuned      SQLAlchemy       Neo4j            Jac (--scale)
app layer  hand SQL          Python ORM       Cypher           walkers + OSP + GTI + FP
storage    PostgreSQL        PostgreSQL       Neo4j native     MongoDB (L3) + Redis (L2) + dict (L1)
```

Consequences:
- Closest peer on the "hide the database" axis = **SQLAlchemy** (both are app-layer-over-a-DB).
- **Parity** with Neo4j (both graph models, index-free-adjacency-like).
- **Below** hand-tuned PG on single-hop raw latency — that's PG's home turf, by construction. Don't stake the claim there.
- The real wins: **multi-hop traversal** (GTI O(1)/hop vs SQL joins/CTEs) and **developer burden**.

---

## 3. Performance thesis (the goal — a hypothesis to TEST, not engineer toward)

PG-handtuned = the **latency floor** (C engine + covering index). Targets:
- single-hop vs PG: **track close** (realistic only with a warm cache — gated on not clearing Jac's cache).
- single-hop vs SQLAlchemy: **slightly beat** (you beat the ORM tax, not the engine — most defensible win).
- single-hop vs Neo4j: parity → slight edge.
- multi-hop vs Neo4j: **match the scaling shape** (both flat; SQL blows up). Matching absolute C latency needs native compile (future).

Build the harness fair, report what falls out. The warm-**L1 hit rate** is the number that decides whether single-hop-vs-PG parity is reachable.

---

## 4. Type-selectivity test design (the big refocus)

**The experiment:** a root connects to a MIX of node types; "X% selectivity" = X% are the target type; each backend fetches just that subset. Jac's edge: OSP/GTI knows the type from topology, never inspects bodies.

**Why the old harness was unfair (verified at code level):** all 3 baselines pre-separated the non-target types *out of the query path* — PG `FROM tweets WHERE author_id` (channels in other tables), SQLA separate `Tweet` model, Neo4j `:POST` rel-type skips `:MEMBER`. Only Jac faced a mixed set → the selectivity axis moved only Jac → a Jac-internal property, not a cross-backend result.

**Fair rule:** the non-target types must **coexist in the structure each backend queries**, and each discriminates by its native **node-type** index — nobody pre-separates types into clean tables/edges. Native mechanisms:
- Jac: GTI/SAM `[root-->(?:Tweet)]`.
- Neo4j: node **labels** via **one generic relationship** → `MATCH (root)-[:CONN]->(n:Tweet)` (don't type the edges or it skips noise free).
- PostgreSQL / SQLAlchemy: see two conditions below.

**Two conditions (workload contract = same result set; method free, per researchers):**
- **OPTIMIZED = primary performance figure.** Each backend's best native modeling: PG/SQLA → **TPT** (separate tables + covering index), Neo4j → edge-type, Jac → GTI. Hardest bar for Jac (PG-TPT = zero type-filter cost).
- **STI = the contrast.** Single-table inheritance + `type` discriminator **with a reasonable composite index** `(owner_id, type)` (isolates *modeling* effort, not a missing index); SQLA polymorphic STI.

**The delta IS the developer overhead.** Jac can't change (OSP/GTI is automatic → ONE number). The others' **(optimized − STI) latency delta** = the payoff of hand-optimization effort Jac never spends. Pair with the **DBLOC** figure (effort) — the delta is the *payoff* of effort, DBLOC is the *effort itself*; anchor the burden claim on DBLOC, use the delta as support. **Test both; researchers/professors own the framing; we guarantee both conditions are internally valid.**

---

## 5. Fair timing design

**Implemented model (2026-06-13) — spec: `docs/specs/2026-06-13-fair-timing-instrumentation.md`.**
Measure both client and server per trial; decompose `server_total` into two sub-phases + residual.

```
ms_fetch + ms_build           two server sub-phases (descriptive, per-backend)
server_total                  handler entry → return (measured independently)
client_total (= latency_ms)   perf_counter around the POST
network_ms = client_total − server_total      derived, NOT measured
```

- **`server_total` is THE comparable cross-backend number** (substrate, network-excluded) — it
  carries the claims. Measured independently (entry→return), not summed, so the residual is visible.
- **`ms_fetch` / `ms_build` are per-backend DESCRIPTIVE, NOT cross-comparable** (see caption rules).
- **`client_total`** = delivered latency (provisional under `port-forward`).
- **`network_ms`** = transport + HTTP framework + **AUTH** (auth runs pre-handler → excluded from
  `server_total`). Derived context, not a claim-bearer. **Never call it "network latency."**

Phases reduced from the original four (`auth/query/fetch/build`) to **two measured sub-phases +
residual**: `ms_auth` dropped (pre-handler framework), `ms_query` dropped (not separable from fetch
on PG, trivial for indexed single-hop). `residual = server_total − ms_fetch − ms_build` = in-handler
glue + any in-handler auth resolve.

### Per-backend marker table (canonical)

| backend | `ms_fetch` = | `ms_build` = | `server_total` = | note |
|---|---|---|---|---|
| postgres | `json_agg` SQL round-trip | `0.0` | entry → return | **built in-SQL, inside `ms_fetch`** |
| sqlalchemy | query exec | `.report()` hydration loop | entry → return | the ORM-hydration tax |
| neo4j | cypher run + `.data()` | list-comprehension build | entry → return | in-handler auth → residual |
| jac | materialize `[my_profile-->(?:Tweet, like_count > 10)]` | dict-append loop | entry → return | measurement seam only; GTI+FP unchanged; `:pub` untouched (§13) |

Uniform envelope: `data.reports[0].server_timing = {ms_fetch, ms_build, server_total}` (jac:
`reports[0].server_timing`). One shared `extract_server_timing` in `base.py`. Missing/unparseable →
blank columns + warn (best-effort; not fail-closed). Invariant: `server_total ≥ ms_fetch + ms_build`.

### Caption rules (for the eventual phase-breakdown figure)

- **Only `server_total` is rankable across backends.** A reader must NOT rank `ms_fetch` or
  `ms_build` segments across backends.
- **PG `ms_build = 0` means "built in-SQL, inside `ms_fetch`," NOT "PG builds for free."**
- Phase-breakdown segments = **`ms_fetch` / `ms_build` / residual / network** (not the old
  `auth/query/fetch/build`).
- `network_ms`: allow negatives (no clamp), mark provisional, report its **distribution** not a
  point median.

**Tools:** lightweight `perf_counter` markers stay in timed runs. cProfile/yappi = separate
diagnostic runs only (never read latency off a profiled run) — the future intra-phase split.

**Deferred (not blocking):** `plot.py` rendering + p50/p95/p99 bands; in-cluster bench client
(only cleans up the provisional `client_total`/`network_ms`); `ms_auth` restoration (only if an
auth claim ever matters).

---

## 6. Cache & warmup

- **All-warm default.** 20 warmup (discarded) / 30 timed trials. Warm-vs-warm is the fair comparison (each backend uses its own native caching). The old sin was warming baselines but cold-clearing Jac every trial.
- **Cache ablation (warm L1 vs cold) = Jac-internal**, folded into the Phase Breakdown figure (the fetch phase shrinks when warm). Run via the `--cold-l1` flag. **Never plot jac-cold against warm baselines** — only as a labeled Jac-vs-Jac ablation.
- Warmup also handles Neo4j's **JVM JIT warmup** (it was jumpy ~12–15 ms with warmup outliers on the cluster — may need more than 20).

---

## 7. Variance & stats

30 trials, **raw per-trial rows persisted** (so spread is recomputable — the old harness kept only aggregates), report **p50/p95/p99** (the median hides the tail where JVM/cache jitter lives). Optionally per-phase variance.

---

## 8. Seeding & correctness

- **Deterministic** (`seed_gen.py`, fixed RNG) → byte-identical, reproducible, and **identical across all backends** (so result sets are comparable — the key improvement; the old harness used random, per-backend-different data).
- **Per-point namespacing, no teardown.** Fresh user/namespace per point; query scoped to it. Teardown would cold-start caches and fight warmup. Caveat: namespace isolation must *work* (the root-isolation bug is exactly this failure) and verify accumulation doesn't drift the numbers.
- **Min target-node floor** — pick fanout/selectivity so the lowest point yields ≥~20–30 target nodes (avoid degenerate selectivity).
- **Correctness oracle (Phase 5, not yet wired):** compare **content + likes + comments multisets** across all 4 backends (NOT counts — the old count check passed the IDs-only payload), fail-closed, and **gate plots** (mismatch → exclude the point). Must run on every backend including Jac.

---

## 9. Figure roster (locked 2026-06-13)

Immediate (single-hop, MongoDB backend now):
1. **DBLOC** — burden bars, FIRST figure. DBLOC primary + LOC secondary; naive+optimized per relational/Neo4j; **Jac = single bar**. Fix the PROJECTED Jac bar (use a real number); define a rigorous uniform DBLOC counting rule.
2. **Fanout** — latency vs neighborhood size; **+ naive-jac reference line** (GTI ablation merged in — rises with fanout while GTI-jac stays flat). Warm, p50/p95/p99.
3. **Type Selectivity** — latency vs target-type fraction; **optimized condition only** (STI gap → fig 4); type discrimination, not FP.
4. **Latency vs DBLOC** — effort×latency scatter; optimized−STI delta; Jac off-the-curve. Latency from **one type-sel point (fanout=1000, ~50%)**, same for all, caption it.
5. **Latency Phase Breakdown** — auth/query/fetch/build + network; **cache ablation folded in** (warm vs cold Jac).

Future: **Filter Pushdown** (field-predicate selectivity), **Multihop** (hop-depth sweep, Phase 4.5), **Write-heavy workload** (near-future — see §11), native compile, profiling, secondary suite, **re-run 1–5 on Postgres-L3**.

---

## 10. Concrete changes to make to the harness (actionable)

- [ ] **Refocus selectivity: FP → TYPE.** Re-introduce mixed-type neighbors off root; PG/SQLA discriminator/polymorphic + TPT (two conditions); Neo4j one generic edge + node-label; Jac `[root-->(?:Tweet)]`. Move the `like_count` predicate to a separate future FP test. *(gates the SELECTIVITY run only; fanout run does not need it)*
- [ ] **Add STI vs optimized conditions** for relational/Neo4j; record implementation **DBLOC/LOC for both** conditions. *(figure 3b / Latency-vs-DBLOC)*
- [ ] **Wire the GTI index ON/OFF ablation** — deploy a second jac with env **`JAC_INDEX_ENABLED=false`** to produce the naive-jac reference line on Fanout + 3b. (Verified deployed-branch source `topo_utils.impl.jac:_is_enabled`: env `JAC_INDEX_ENABLED` overrides, else falls back to `jac.toml topology_index`. The env is **process-level** — a separate deploy, not a per-request toggle. The earlier "use JAC_TOPOLOGY_INDEX / JAC_INDEX_ENABLED is a no-op" claim was WRONG for this build.)
- [x] ~~**Split `ms_fetch` vs `ms_build`** server-side spans~~ — **DONE** (commit `d1ebf3f`, Approach A). Reduced to 2 phases + residual; `ms_auth`/`ms_query` intentionally folded (see §5).
- [ ] **Run the bench client in-cluster** over the Service; kill `port-forward` for measured runs. *(DEFERRED — only cleans up provisional `client_total`/`network_ms`; `server_total` carries the claims)*
- [ ] **Emit p50/p95/p99** in `plot.py`; bands on the figures. *(post-run; recomputable from saved rows)*
- [ ] **Wire the Phase-5 content oracle** + plot-gating.
- [x] ~~**Min target-node floor** in the seed grid~~ — **DONE** (commit `d65e9d1`, L2: fanout selectivity 5%→25%, SPEC_VERSION→2).
- [x] ~~**Equalize pod CPU/mem**~~ — **DONE** for the 3 manifest backends (commit `1dbdb36`, identical 1000m/1Gi Guaranteed QoS). ⚠ the jac `--scale` pod still needs on-cluster pinning.
- [x] ~~**Fix the jac root-isolation bug**~~ — **DONE in code** (commit `05a88a2`, dropped `:pub`); cluster isolation-test verify pending (§13).

---

## 11. Backend phasing + write-heavy (professor-raised)

- **MongoDB now; Postgres-L3 later.** Build figures 1–5 on the current Mongo-backed Jac; once a Postgres-L3 jac-scale backend exists, **re-run 1–5** on it. The PG-L3 re-run makes **Jac-vs-SQLAlchemy a pure app-layer-overhead comparison** (same storage engine).
- **Write-heavy (near-future priority).** Current figures are all read-heavy. This is a real bias: **the GTI is built at write time**, so read-only hides the cost side of Jac's read advantage (classic read-optimized-index tradeoff). The write ops already exist as untimed seed code — just time them. Add: write latency/throughput per op (index-maintenance cost isolated), and a **mixed read/write workload (YCSB-style)**. Repurpose Fanout/Phase-Breakdown/Latency-vs-DBLOC; Type-Selectivity has no write analog.

---

## 12. Anti-confusion — what NOT to do

- ❌ Don't parameterize a pushed-down predicate threshold (kills FP silently — §1).
- ❌ Don't use TPT/separate-tables as the *only* relational model (it sidesteps type discrimination) — but DO test it as the *optimized* condition.
- ❌ Don't plot jac-cold or jac-naive against the warm/optimized baselines on cross-backend figures — only as labeled Jac-vs-Jac ablation lines.
- ❌ Don't time through `kubectl port-forward`.
- ❌ Don't conflate type selectivity with filter pushdown.
- ❌ Don't reference `db-replicate` (being purged) or trust any number from it.
- ❌ Don't claim "small constant factor" universally — single-hop is PG's turf; lead with multi-hop + dev-burden.

---

## 13. Open blocker

**Jac root-isolation bug — ROOT CAUSE PINNED 2026-06-13 (it's OURS, a one-word fix).** Symptom: a freshly-registered user's `load_own_tweets` returns a *previous* user's tweets — users aren't isolated by `root`. Breaks per-point namespacing AND corrupts the per-root GTI.

**Cause (verified against the DEPLOYED branch source — see re-verification below):** the benchmark walkers are declared **`walker:pub`** (`server.jac:886, 953, 924, 880`). On the deployed `cse584-W26/jaseci@filter_pushdown` build, the HTTP walker callback gates JWT validation on `requires_auth` (`serve.endpoints.impl.jac:98-128`): `requires_auth = is_auth_required_for_walker(walker_name)`, and the token is **only read when `requires_auth` is true**. `:pub` = public = no auth → `requires_auth=false` → **the JWT is never read** (even though the harness sends a valid one) → `username=None` → `spawn_walker(..., username or Con.GUEST.value)` spawns as **GUEST** → no user_root → context **defaults to `system_root`** (`context.impl.jac:22`). So every `:pub` call runs on one shared `system_root` regardless of which JWT is sent → all users collide.

**⚠ Re-verification (2026-06-13) — settled across two contradicting branches.** A rigorous re-check on `Aarosunn/jaseci@main` *refuted* the `:pub` cause: `main` has a newer **global `jwt_validation_middleware`** that binds root on every request regardless of `:pub` (there, dropping `:pub` would be a no-op). **But `main` is NOT deployed.** The deployed `filter_pushdown` branch has **no global middleware** (grep: zero `middleware`/`bearer`/`user_root` hits in its `jfast_api.impl.jac`); auth lives only in the `requires_auth`-gated callback at `:103`. The earlier pin cited `serve.endpoints.impl.jac:629-633` (the *websocket* handler) — wrong line, right mechanism; the real HTTP walker gate is `:103`. The competing "`aset_user_root` silent-fallback" hypothesis does NOT apply — no such path exists on `filter_pushdown`; the username is simply never extracted for `:pub` walkers. **Net: the pin is correct for the deployed build; drop `:pub` is the verified fix.**

**NOT** jac-cloud's fault (jac-scale isolates correctly when auth IS required), **NOT** the harness (token is refreshed per point, `base.py:90,98`; distinct usernames `harness.py:314`).

**Fix — APPLIED (commit `05a88a2`):** dropped `:pub` from the data-plane walkers (load_own_tweets / seed_tweets / clear_cache / get_all_profiles / import_data); only `health` stays public. No harness change (token already sent). The `grant(ConnectPerm)` calls were **left intact** — a red herring for THIS bug but **load-bearing for the cross-root follow graph** (Phase 4.5): they authorize the `Follow` edge from one root's Profile into another's (see [[jac-cross-root-follow-concern]]). Isolation regression test added (`tests/test_jac_isolation.py`: register A → seed → register B → assert B's `load_own_tweets` empty; skips unless `JAC_BENCH_URL` set). **Remaining:** confirmed against deployed *static* source; the one open step is running that test against the live pod after redeploy.
