# Selectivity → Type-Selectivity Reconciliation Spec (#9)

**Status:** spec, ready to implement — **implementation gated** (see §13 Sequencing).
**Date:** 2026-06-13.
**Scope:** redefine the `selectivity` sweep from a field-predicate (filter-pushdown) into a
**TYPE-selectivity** workload, deliver the **optimized** path per backend, and reseed for a
mixed-type neighborhood. Adds a second selectivity *mode* (`fixed-target` default, `fixed-total`
repro). Produces the optimized figure (fig3 / 3a) and a reproduction/confound figure.
**Out of scope (named, not designed):** the STI/unoptimized counterparts + the 8-line opt/unopt
fig3b (`#2`/`#14`); the naive-jac (`JAC_INDEX_ENABLED=false`) line (`#10`); the filter-pushdown
figure (`#21`). Seams for `#2`/`#14` (§11) and `#21` (§10) are marked; their internals are not
specified here.
**Companions:** `seed-design-spec.md` (the single-hop seed contract this revises),
`harness-fix-spec.md` (how `main()` consumes seeds), `HARNESS_CONTEXT.md` (harness map).
**Design memories:** `type-selectivity-design`, `gti-fp-mechanism`, `seed-contract`,
`figure-roster`, `performance-thesis`, `future-roadmap`.

---

## 1. The confound being fixed

The current `selectivity` sweep is **filter-pushdown mislabeled as type-selectivity**. Every
backend runs a `like_count > 10` field predicate inside the timed fetch; the sweep slides the
seed's % of matching tweets. Verified sites:

| backend | site | current selectivity query |
|---|---|---|
| jac   | `servers/jac/server.jac:903`            | `[my_profile-->(?:Tweet, like_count > 10)]` |
| neo4j | `servers/neo4j/src/main.py:158`         | `MATCH (p)-[:POST]->(t:Tweet) WHERE t.like_count > 10` |
| pg    | `servers/postgres/src/routes/walker.py:521` | `WHERE t.author_id = %s AND t.like_count > 10` |
| sqla  | `servers/sqlalchemy/src/routes/walker.py:316` | `.where(Tweet.author_id==…, Tweet.like_count > 10)` |

The old published fig6 has the same disease at the data level. From the reproduced run
`littleX/results/reproduced_jac_20260529/jac_gtifp_selectivity_sweep.csv`:

```
fan_out  n_tweets  n_channels  selectivity_pct   on_median_ms
1000     20        980         2.0               1.105
1000     50        950         5.0               2.877
1000     100       900         10.0              3.707
1000     200       800         20.0              7.123
1000     300       700         30.0              10.447
1000     500       500         50.0              17.417
1000     750       250         75.0              26.038
```

`fan_out` (total neighborhood = tweets + channels) is **fixed at 1000**; "selectivity" merely
slides `n_tweets` 20→750 inside that fixed total. So the selectivity axis **is** the
returned-set size — the same variable the fanout sweep moves. The GTI-on median rising 1.1→26.0
ms is returned-set growth, not a selectivity effect. **That is the confound.**

The predicate confounds the **fanout** sweep too (it measures "fetch the N that pass a
predicate," not "fetch N"). Therefore the predicate is dropped from the timed primitive
everywhere, not just for selectivity.

## 2. One primitive, three orthogonal axes

`load_own_tweets` becomes a single neutral primitive: **return all of the caller's own tweets**,
no field predicate. Three orthogonal experiment axes ride on it, each realized purely by the
seeded data (the request body stays `{}` — a sweep point is a dataset, not a request parameter,
per `harness.py:8`):

| axis | varies | channels seeded | threshold | returns |
|---|---|---|---|---|
| **fanout**       | N tweets                     | none | off | all N tweets |
| **type-sel**     | channel noise (target fixed/total fixed) | yes | off | all target tweets |
| **filter-push** (`#21`, future) | threshold `k` | n/a | on | tweets with `like_count > k` |

For this spec, **threshold is off on every backend** (no predicate). The threshold seam is §10.

## 3. Type-selectivity workload semantics

The eval user's neighborhood is **mixed-type**: target nodes (`Tweet`, via the post edge) and
noise nodes (`Channel`, via the membership edge) hang off the same user/root. "Selectivity %" =
the target-type fraction of the neighborhood. Each backend must return **only** the Tweet subset,
discriminating the target type via its **native node-type mechanism** — nobody filters on a body
field, and (in the optimized condition) each backend uses its best native type modeling.

The noise must genuinely coexist in the structure each backend queries, so that the type
discrimination is real work (not pre-erased). The optimized baselines pre-separate the noise via
their native modeling (separate tables / a distinct edge type); jac discriminates by node type
through the GTI. That asymmetry is the point of the figure (§12), not a flaw.

**Noise node type = `Channel`** (resolved default). It already exists on all four backends and
needs no new node/table type:

| backend | target edge → Tweet | noise edge → Channel |
|---|---|---|
| jac   | `:Post`   (`server.jac:988`)  | `:Member` (`create_channel`, `server.jac:51` node) |
| neo4j | `:POST`   (`main.py:115`)     | `:MEMBER` (`main.py:138`) |
| pg    | `tweets.author_id` table     | `channel_members` table (`schema.sql:130`) |
| sqla  | `Tweet.author_id`            | `channel_members_table` (`models.py:137`) |

## 4. Per-backend optimized query (predicate-drop only for baselines)

Threshold off → drop the `like_count > 10` predicate. The optimized baselines already
pre-separate the noise, so for them this is a one-line deletion:

- **jac** (`server.jac:903`): `[my_profile-->(?:Tweet)]`.
  **LOAD-BEARING CONSTRAINT — the edge must stay untyped `-->`.** `-->` traverses *all* out-edges
  (Post **and** Member) and the `?:Tweet` clause discriminates by **node type** via the GTI
  `n:Tweet` bucket (O(1), skips the `n:Channel` bucket entirely). This is genuine
  *node-type* discrimination. An edge-typed traversal (`[my_profile->:Post:->(?:Tweet)]`) would
  discriminate by **edge type** and skip the channel noise for free even with the GTI **off** —
  which would silently defeat the future naive-jac line (`#10`/3b). Keep it `-->`.
- **neo4j** (`main.py:157-163`): `MATCH (p:Profile {jac_id:$uid})-[:POST]->(t:Tweet) RETURN …` —
  delete the `WHERE t.like_count > 10` line (`main.py:158`). The `:POST` edge type pre-separates
  the `:MEMBER`→`:Channel` noise (its native optimized modeling).
- **pg** (`walker.py:496-522`): delete `AND t.like_count > 10` from the WHERE (`walker.py:521`).
  TPT — `channel_members` is a disjoint table the query never touches; the covering index
  `(author_id, created_at DESC) INCLUDE (content, like_count)` (`schema.sql:65`) stays.
- **sqla** (`walker.py:314-323`): delete `Tweet.like_count > 10` from the `.where(...)`
  (`walker.py:316`). TPT — channels in `channel_members_table`, never touched.

The payload shape (content + likers + comments) is unchanged on every backend; `response_bytes`
parity and the content oracle are unaffected (channels never appear in the response).

The `server_timing` block (fair-timing spec) is unchanged — this spec only changes *which rows*
the fetch returns, not the instrumentation.

## 5. Two selectivity modes + sweep tables

A new harness flag **`--selectivity-mode {fixed-target,fixed-total}`**, default **`fixed-target`**.
Both modes share the same seed *shape* (target tweets + channel noise off the eval user); only the
per-point counts differ.

### 5.1 fixed-target (DEFAULT — the corrected figure, fig3)

`n_tweets` is held **constant at 1000**; channel noise grows as the target fraction shrinks:

```
n_channels = round( n_tweets * (1 - s) / s )     where s = selectivity_pct / 100
```

Returned set is **constant (1000 tweets)** → any latency slope isolates pure type-discrimination
cost. Optimized baselines + jac-GTI → expected **flat**.

| selectivity_pct | 10 | 20 | 30 | 50 | 75 | 100 |
|---|---|---|---|---|---|---|
| n_tweets        | 1000 | 1000 | 1000 | 1000 | 1000 | 1000 |
| n_channels      | 9000 | 4000 | 2333 | 1000 | 333  | 0    |
| total nodes     | 10000 | 5000 | 3333 | 2000 | 1333 | 1000 |

Point rationale (locked): floor at **10%** (9:1 junk:target already proves "flat as noise grows";
5% would need 19k channels off one user and forces chunked seeding for marginal gain); **100%** is
the **zero-noise flatness anchor** — the reference height the noisy points are compared against,
not degenerate. The low-sel extremes (2%, 5%) live on the `fixed-total` curve only (§5.2), where
they're cheap and where the confound demo needs them.

### 5.2 fixed-total (REPRODUCTION — the confound figure)

Total neighborhood held **constant at 1000**; `n_tweets = s * 1000`, `n_channels = 1000 - n_tweets`.
This mirrors the old harness **row-for-row** (the §1 CSV). Returned set grows with selectivity →
jac-GTI **rises**, reproducing the old fig6 shape on the new harness.

| selectivity_pct | 2  | 5  | 10  | 20  | 30  | 50  | 75  |
|---|---|---|---|---|---|---|---|
| n_tweets        | 20 | 50 | 100 | 200 | 300 | 500 | 750 |
| n_channels      | 980 | 950 | 900 | 800 | 700 | 500 | 250 |
| total nodes     | 1000 | 1000 | 1000 | 1000 | 1000 | 1000 | 1000 |

Channel count maxes at 980 (sel=2%) → the full 7-point repro is **cheap** (no chunked seeding).

### 5.3 The confound figure (no naive line needed)

Overlay the two modes' **optimized-jac (GTI-on)** curves on the **shared points [10,20,30,50,75]**:
`fixed-total` rises (returned-set grows), `fixed-target` is flat (returned-set constant). Same
selectivity axis, opposite shapes → the old rise was **fanout, not selectivity**. This needs only
GTI-on in both modes — the naive (GTI-off) line and the speedup story stay future (`#10`/3b).

## 6. Seed changes — `seed_gen.py`, SPEC_VERSION 3

Bump `SPEC_VERSION` **2 → 3** (`seed_gen.py:17`). The contract changes: the predicate is gone, and
selectivity points carry channel noise. Regenerate all seed files.

### 6.1 Spec schema additions

Selectivity point specs gain a **`channels`** array (the noise) and a **`selectivity_mode`** field;
the existing `tweets` array is the target set. Fanout points carry no `channels` (empty/omitted).

```json
{
  "spec_version": 3,
  "sweep_type": "selectivity",
  "selectivity_mode": "fixed-target",      // "fixed-target" | "fixed-total"
  "param_value": 10,
  "n_tweets": 1000,
  "n_channels": 9000,
  "selectivity_pct": 10,
  "channels": [ {"key": "ch_0000", "name": "channel 0"}, ... ],   // len == n_channels
  "tweets": [ ... ]                          // unchanged target-tweet schema (seed-design-spec §3)
}
```

- `channels[].key` = stable cross-backend identity (mirrors tweet `key`); `name` is filler.
- Channels carry **no payload that reaches the response** — they exist only as neighborhood noise.
- Per-mode/point counts per §5 (`n_tweets`, `n_channels`). RNG provenance string includes the
  mode: `v3:selectivity:fixed-target:10`. Fixed draw order; never reorder without bumping
  SPEC_VERSION.

### 6.2 Seed file naming (resolved default)

```
seed/fanout_{N}.json
seed/selectivity_fixed-target_{pct}.json
seed/selectivity_fixed-total_{pct}.json
seed/manifest.json
```

`manifest.json` lists every point with its `sweep_type`, `selectivity_mode`, `param_value`,
`n_tweets`, `n_channels`.

### 6.3 fanout sweep — stop forcing 25%

The fanout seed's "fixed selectivity at 25%" (`seed_gen.py:25` `FANOUT_SELECTIVITY_PCT = 25`) was a
**predicate artifact** — it existed only so the dropped `like_count > 10` predicate returned a
defined fraction. With the predicate gone, **fanout returns all N tweets** (effectively
selectivity 100%, zero channels). Remove the 25% like-distribution forcing from the fanout path;
`like_count` is still seeded per-tweet for payload realism and the future FP test, but it no longer
gates the returned set. Fanout point shape is otherwise unchanged.

### 6.4 Per-backend seed-endpoint changes (channels + memberships)

Each backend's seed endpoint must create the channel noise and attach it to the eval user.
Channels are created **untimed** (seed path), like tweets. "NEW" = behavior to add:

- **jac** `seed_tweets` (`server.jac:961`): accept a `channels` list; for each, create a
  `Channel` node and a `:Member` edge from the eval `my_profile` (mirror the tweet/`:Post` loop,
  `server.jac:987-998`). **VERIFY-AT-IMPLEMENTATION (§15): fixed-target @10% creates ~9,000
  `Channel` nodes + `:Member` edges in a SINGLE walker call** — confirm that completes under the
  jac-cloud request timeout (seed-design-spec §8 jac volume note flags the same risk at ~15.5k
  likes). **If it does not, chunk the channel seed (e.g. batches of 1000) — the seed path already
  tolerates multiple calls per point.** Do not let a timed point run on a partially-seeded
  neighborhood.
- **neo4j** `seed_tweets` (`main.py:216`): accept `channels`; `UNWIND $channels AS ch CREATE
  (p)-[:MEMBER]->(:Channel {...})` batched (mirror the tweet UNWIND, `main.py:239-248`).
- **pg** `seed_tweets` (`walker.py:620`): insert `channels` rows + `channel_members` rows for the
  eval user, one transaction (mirror the tweet/likes inserts).
- **sqla** `seed_tweets`: insert `Channel` rows + `channel_members_table` rows for the eval user
  (bulk insert, mirror tweets).

Post-seed verification (harness-fix-spec §1.4) extends to assert `n_channels` memberships exist for
the eval user before any timed point.

## 7. Harness changes — `harness.py`

- Add `--selectivity-mode {fixed-target,fixed-total}`, default `fixed-target`.
- `SWEEPS["selectivity"]` (`harness.py:54`) becomes mode-dependent:
  - `fixed-target` → `[10, 20, 30, 50, 75, 100]`
  - `fixed-total`  → `[2, 5, 10, 20, 30, 50, 75]`
- Selectivity points load the mode-matching seed files (§6.2).
- `SWEEP_ENDPOINTS` (`harness.py:61`) unchanged — both modes hit `load_own_tweets` (the one
  primitive, §2). No request-body change.
- CSV: record `selectivity_mode` alongside `sweep_type`/`param_value` so a results file is
  self-describing and the two modes never collide on `(sweep_type, param_value)`.

## 8. Figures — `plot.py`

- **`fig3_type_selectivity`** — `fixed-target`, the corrected 6-point figure (3a). Four clean
  optimized lines (jac-GTI, pg, sqla, neo4j). Headline: "is jac competitive at everyone's best?"
- **`fig3_repro_confound`** — overlay of the two modes' optimized-jac curves on the shared
  `[10,20,30,50,75]` (§5.3): `fixed-total` rising vs `fixed-target` flat.
- Rename `figFP_selectivity_provisional` (`plot.py:224`) → these. Update the `_AXIS_LABEL`
  (`plot.py:32`) from `"Selectivity (% of own tweets matching like_count > 10)"` →
  **"Type selectivity (% of neighborhood that are tweets)"**, and the title/caption
  (`plot.py:37-43`) to drop the `like_count` framing.
- Both modes run **all four backends** (resolved default; harness is backend-agnostic). The
  confound figure focuses the jac contrast; the baselines are plotted for context.
- **Caption rule:** `fig3_repro_confound` must state that it reproduces the *method* of the old
  fig6 to expose the selectivity/fanout confound — it is a methodological artifact, not a
  performance claim.

## 9. Sweep value cross-reference

| mode | points | n_tweets | n_channels | returned-set | jac-GTI shape |
|---|---|---|---|---|---|
| fixed-target | 10,20,30,50,75,100 | 1000 const | `1000*(1-s)/s` | constant | flat |
| fixed-total  | 2,5,10,20,30,50,75 | `s*1000`   | `1000-n_tweets` | grows | rises |

## 10. Seam: threshold param / filter-pushdown (`#21`, future) — with the honest jac caveat

The dropped predicate becomes an **optional threshold parameter** on the same `load_own_tweets`,
default **off** (no predicate; what fanout + both selectivity modes use). This is the seam so the
future FP sweep (`#21`) is mostly a harness+seed change.

- **Baselines (pg/sqla/neo4j): zero new server code.** A bound `threshold` param adds
  `like_count > :k` to the SQL/Cypher WHERE; the bind value still pushes down to the index — set
  the param now (default off → no WHERE clause), exercise it in `#21`.
- **jac: the param does NOT make FP free — honest caveat.** Verified in the jaseci
  `filter_pushdown` source (`gti-fp-mechanism`): `extract_query_filter` extracts a predicate
  **only when the comparison RHS is a literal**. A walker-variable RHS (`like_count > self.k`) is
  **silently skipped** → no pushdown → `filtered_bulk_get` falls back to loading every
  GTI-resolved node and filtering in Python → it measures the *non-FP* path while looking like FP.
  So **jac-FP (`#21`) requires a fixed-LITERAL predicate in the comprehension + sweeping the seed
  data distribution at that fixed literal** (per `seed-contract`: "sweep selectivity via the seed
  data at a fixed literal threshold — never parameterize the threshold"), **not** a runtime
  param. That is a small literal-variant on jac, not zero code.
- **Type-selectivity (this spec) is unaffected:** threshold is off on every backend, so no
  predicate runs and the jac caveat never bites here. The caveat is documented so `#21`'s
  implementer does not naively wire `self.k` and silently null jac's FP.

## 11. Seam: STI / unoptimized condition + fig3b (`#2`/`#14`, future) — not designed here

This spec delivers the **optimized** condition only (each backend at its best native type
modeling). The future `#2`/`#14` adds the **unoptimized/STI** condition and the 8-line opt/unopt
`fig3b`. Where it plugs in:

- The seed's **channel noise nodes are already created** (§6) and are consumed unchanged by both
  conditions — `#2` adds no new seed shape, only new per-backend query/schema variants that are
  *forced to co-locate and discriminate* the noise instead of pre-separating it:
  pg single-table + `type` discriminator + `(owner_id, type)` index; sqla polymorphic STI; neo4j
  generic edge + node-label scan; jac = naive (`JAC_INDEX_ENABLED=false`, the `#10` deploy).
- The harness gains an opt/unopt toggle; `fig3b` overlays solid (opt) vs dashed (unopt) per
  backend; jac's dashed line is the naive-jac from `#10`.
- The caption honesty seam (`type-selectivity-design`): the opt→unopt gap is hand schema-modeling
  effort for the baselines but automatic/zero-effort GTI for jac — anchor the dev-burden claim on
  DBLOC (fig1), use the gap as supporting evidence.

Do not implement any of this here.

## 12. Fairness argument (for the figure captions / paper)

The old harness moved the selectivity axis for **jac only**: all baselines pre-separated the
non-target types into clean tables/edges, so the channel noise was invisible to them and the
"selectivity" curve was a jac-internal naive-vs-GTI property, not a cross-backend comparison
(`type-selectivity-design`, audit §4a). This spec makes the noise **coexist in every backend's
structure** and has each discriminate by its native node-type mechanism. In the optimized
condition that means the baselines legitimately pre-separate (TPT / edge-type) — their best native
modeling — and jac discriminates by node type through the GTI. `fig3` then answers a fair question:
*at everyone's best, does jac's automatic type discrimination stay competitive?* The effort the
baselines spend to model that separation is the future `fig3b`/DBLOC dev-burden story (§11).

### 12.1 Likes/comments assembly — methodological note (per-backend, NOT uniform)

Each backend assembles a tweet's likers and comments via its **idiomatic-optimal** model, **not**
a uniform one — do not claim "all backends denormalize," the code disproves it:

- **jac** — denormalized `likes: list[str]` on the `Tweet` node, read inline (the canonical
  littleX idiom).
- **neo4j** — denormalized array property `t.likes`, read inline.
- **postgres** — *normalized* `likes` table, assembled set-based via a correlated subquery inside
  one `json_agg` round-trip.
- **sqlalchemy** — *normalized* relationship via batched `selectinload` + ORM hydration.

None performs a client-side per-tweet **N+1**. The prior jac implementation reverse-walked `Like`
edges once per tweet — the repo's *fullstack-app* model, used by **no** benchmarked backend; it was
replaced with the denormalized field to (a) match jac's own canonical idiom and (b) drop an N+1 the
comparators don't pay (measured: a ~7× cut to jac's dominant `ms_build` phase, `server_total` 81ms→
~20ms at fanout=1000) — **fairness, not advantage**. This is an intentional *"each system at its
idiomatic best"* comparison: the graph/document backends use denormalized arrays, the relational
backends use normalized joins, and the cost difference is **surfaced, not hidden**, by the
fair-timing `ms_fetch`/`ms_build` split (timing spec §3) — it is part of what the benchmark measures.
A model-held-constant variant (denormalized-everywhere or normalized-everywhere) is a separate future
axis (cf. STI/TPT, §11 / `#2`). The `Like` edges are **retained** in jac (social graph / Phase 4.5
multi-hop); `load_own_tweets` simply no longer traverses them. Write-side: the denormalization makes
jac's `like_tweet` double-write (field + edge) — the standard read-cost-for-write-cost trade,
relevant to the future write-heavy workload (`#22`).

## 13. Sequencing / coordination (implementation is gated)

- **Do NOT interrupt the in-flight clarity fanout run.** It runs on current predicate-bearing
  code; its job right now is pipeline validation, and its predicate-bearing numbers are
  FP-confounded — they were always going to be re-derived.
- The predicate-drop (§4) + reseed (§6) is an **implementation-phase change, applied after the
  clarity fanout run is green**, per the locked no-conflict sequencing.
- Order: in-flight fanout finishes → (this spec written, paused) → implement predicate-drop +
  type-sel reseed → **re-run fanout clean** (return-all-N) and run both selectivity modes.
- A parallel session owns the content oracle; channels never enter the response, so the oracle's
  tweet-set comparison is orthogonal to this change (§4).

## 14. Resolved defaults (summary)

1. **Noise type = `Channel`** (reuse existing on all four backends).
2. **Both modes run all four backends**; the confound figure focuses the jac contrast.
3. **Seed-file naming** per §6.2; **SPEC_VERSION → 3**; regenerate all files.
4. **Plot:** `fig3_type_selectivity` + `fig3_repro_confound`; axis relabel to
   "Type selectivity (% of neighborhood that are tweets)".

## 15. Verify-at-implementation

1. **fixed-target @10% seeds ~9,000 `Channel` nodes + `:Member` edges off ONE eval user in a
   single jac `seed_tweets` call.** Confirm it completes under the jac-cloud request timeout
   (seed-design-spec §8). If not, **chunk the channel seed (e.g. batches of 1000)** even at 9k —
   never run a timed point on a partially-seeded neighborhood. (fixed-total maxes at 980 channels
   → no chunking needed.)
2. Confirm the jac `-->` untyped traversal still resolves the GTI `n:Tweet` bucket with the
   predicate removed (§4) — i.e. dropping the `like_count > 10` clause does not change the typed
   comprehension's GTI engagement, only the returned rows.
3. Byte-determinism of regenerated seeds (generate twice, compare digests) as in seed-design-spec
   §4, now including the `channels` arrays and the mode in the RNG provenance string.

## 16. Traceability

| audit / memory finding | how this spec answers it |
|---|---|
| selectivity-axis-mislabeled (FP-as-type) | type-selectivity = mixed-type neighborhood, native node-type discrimination (§3, §4) |
| selectivity/fanout confound (old fig6) | predicate dropped from the shared primitive; `fixed-total` reproduces + `fixed-target` corrects; confound figure overlays them (§1, §5) |
| baselines pre-separate types → flat axis | noise coexists in every backend; optimized = legitimate native modeling, gap deferred to fig3b (§11, §12) |
| jac literal-RHS FP gotcha | threshold seam documents that jac-FP needs a fixed literal + seed sweep, not a runtime param (§10) |
| fanout "25% selectivity" artifact | removed; fanout returns all N (§6.3) |
