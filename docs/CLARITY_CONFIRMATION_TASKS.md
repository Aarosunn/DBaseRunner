# Clarity Confirmation Tasks — self-contained handoff

**You are a fresh Claude session running on the `clarity` cluster host (minikube + the
jac-scale self-deploy). You have no shared memory or plugins — everything you need is in
this doc + the repo.** Your job: deploy + run the smoke-tests/experiments below, record
PASS/FAIL with evidence, and report back. Do NOT redesign the experiment; if something is
ambiguous, note it and continue.

Base deploy mechanics live in `docs/RUN_PLAYBOOK.md` (deploy flow, gates, scp). This doc
adds **what changed** and **what to confirm**.

---

## 1. Context — what this is and why

- **Project:** `DBaseRunner` benchmarks 4 backends (jac / postgres / sqlalchemy / neo4j) on a
  single-hop workload `POST /walker/load_own_tweets` (a profile's own tweets + inline
  likes/comments). One CSV row per trial; `plot.py` renders the figures.
- **Thesis (current):** *GTI + Filter Pushdown offload query work onto the language runtime to
  make DB requests faster.* We are isolating jac's performance into **GTI** vs **cache layers**
  via ablations (below).
- **KEY FINDING driving the ablations:** jac's **L1 in-process anchor cache (`__mem__`) persists
  across HTTP requests** (it is NOT cleared per request). After warmup, warm jac serves reads
  straight from process memory — **never touching Redis or Mongo on reads.** Postgres, by
  contrast, executes a real query every request. So a warm `server_total` comparison partly
  measures *cache residency vs query execution*, not the GTI mechanism. **To isolate GTI/FP you
  must run `--cold-l1`** (clears `__mem__` per trial so jac actually fetches from storage).

## 2. What changed in this commit (already applied — you're pulling it)

- **`servers/jac/jac.toml`** repointed the deployed jaseci source:
  - was: `cse584-W26/jaseci @ filter_pushdown`
  - now: **`https://github.com/Aarosunn/jaseci.git @ fp-dirtyfix`**
  - **Why:** `filter_pushdown` had a broken `ScaleTieredMemory.commit` that re-flushed **every**
    L1 anchor (including read-only ones) at request close → ~7s client latency ("writes-on-read").
    `fp-dirtyfix` = filter_pushdown + a backported **hash dirty-check**: `_compute_hash` added to
    the serializer, `anchor.hash` set at deserialize, and the commit flush now skips anchors whose
    recomputed hash is unchanged (read-only → skipped; mutated/new → flushed). GTI is present
    (`topology_index.jac`); FP code is dormant (no predicate in the query). **NB: we did NOT use
    `Aarosunn/jaseci@main` — its `jac-scale` was repackaged to `jac.toml` (no `pyproject.toml`), so
    the deploy's `pip install ./jac-scale` can't build it (that was the A4 CrashLoop).**
- **`harness.py`** new `--label` flag (defaults to `--backend`). Lets you run the SAME backend
  under several configs and tag each run's CSV/figure distinctly (for the 4 jac ablation lines).
  `--backend` still selects the class + the jac-only `--cold-l1` path.
- **`plot.py`** added line styles for `jac_nogti`, `jac_noredis`, `jac_noredis_cold`.

## 3. The 4 jac ablation conditions (the experiment)

| Label (`--label`) | GTI | L1 | Redis | How to produce |
|---|---|---|---|---|
| `jac` (#1 full) | ✓ | warm | ✓ | default deploy, warm run |
| `jac_nogti` (#2) | ✗ | warm | ✓ | deploy with GTI off, warm run |
| `jac_noredis_cold` (#3) | ✓ | cleared | ✗ | default deploy + `--cold-l1` (the bundled `clear_cache` walker clears L1 AND closes Redis) |
| `jac_noredis` (#4) | ✓ | warm | ✗ | deploy with Redis disabled, warm run |

Interpretation: **#1 vs #2** = what GTI buys. **#1 vs #3** = what caching buys. **#3** = the
honest "GTI-vs-SQL with all caching stripped" number.

---

## 4. CONFIRMATION TASKS — do these in order, record PASS/FAIL + evidence

### A. The repoint works (gates everything) — after rebuild + redeploy
> Deploy jac fresh so it clones `Aarosunn/jaseci@main`. Per playbook:
> `cd servers/jac && ./teardown.sh; ./deploy.sh` (deploy.sh runs `jac start --scale`, which
> clones+builds the pinned repo). If a stale image is cached, force a clean rebuild.

- **A1 — GTI engages.** After deploy, seed a tiny dataset and confirm reads return tweets:
  - `kubectl exec $(kubectl get pod -l app=jaseci -o name | head -1) -- printenv | grep -i JAC_INDEX_ENABLED` → empty or `true` = GTI ON (good); `false` = OFF.
  - Run the harness on a 1-point fanout (below) → `load_own_tweets` returns the seeded tweet count. **PASS = non-empty, correct count.**
- **A2 — Writes persist (dirty-check correctness).** This is the risk of the new commit fix: if
  the hash isn't set on load, mutations could be silently dropped. Seed N tweets, then
  `load_own_tweets` → **PASS = returns all N** (not 0, not short). Also like a tweet, re-read →
  the like is still there.
- **A3 — Client latency back to normal (the 7s bug is gone).** Run one fanout=1000 point. In the
  CSV, compare `latency_ms` (client) vs `server_total_ms`. **PASS = `latency_ms` is NOT ~7000ms**
  (should be within a small multiple of `server_total_ms` + network, not seconds). On the old
  `filter_pushdown` branch this was ~7s; it should now be ~tens of ms.
- **A4 — No other deploy breakage.** `fp-dirtyfix` = filter_pushdown + 3-file dirty-check patch,
  so it should build like filter_pushdown always did. **PASS = deploy reaches Ready, `/docs`
  returns 200, auth + seed + load all work.** If it breaks, capture `kubectl logs -l app=jaseci`
  and FALL BACK to the known-good **`jaseci_repo_url=https://github.com/cse584-W26/jaseci.git`,
  `jaseci_branch=filter_pushdown`** (builds fine, but WITHOUT the fix → ~7s `latency_ms`; that's
  client-side only, so `server_total` and the ablation are still valid — just note it).

### B. The ablation knobs
- **B5 — GTI-off deploy (#2).** Two routes, try the env first:
  - Env: after deploy, `kubectl set env deployment/<jaseci-deploy> JAC_INDEX_ENABLED=false`
    (pod restarts), reconfirm via `printenv`. OR
  - Config: set `[run] topology_index = false` in `servers/jac/jac.toml` and redeploy.
  - **PASS = `printenv` shows `JAC_INDEX_ENABLED=false`** AND reads still return correct tweets
    (GTI off = naive traversal, slower but correct). Record which route worked.
- **B6 — No-redis deploy (#4). ⚠️ NEEDS INVESTIGATION.** `jac start --scale` auto-provisions
  Redis; there is no known clean toggle. Find how to deploy with **no Redis / `redis_url` unset**
  so `ScaleTieredMemory.init` sets `l2 = None` (check `redis.impl.jac` / the scale config /
  whether deleting the redis pod+service makes the app fall back to `l2=None` instead of
  crashing). **Report what you find** — if there's no clean way, say so; this condition may be
  deferred. Confirm via logs ("Redis not available, running without distributed cache").
- **B7 — `--cold-l1` runs clean (gates #3).** The `clear_cache` walker (`servers/jac/server.jac`)
  clears L1 **and** closes Redis (L2) and is known NOT to reopen L2. Run a tiny sweep with
  `--cold-l1` and confirm it **completes without 500s/crashes** (degrades to L3/Mongo reads).
  **PASS = run finishes, rows written, no repeated 500s.** If it crashes, capture the error.

### C. Type-selectivity actually runs
- **C8 — fixed-total seeds on jac.** The selectivity sweep in `fixed-total` mode caps the
  neighborhood at ~1000 nodes (vs `fixed-target` which can hit 10k channels and hang jac's single
  worker). Run `--sweep selectivity --selectivity-mode fixed-total` on jac → **PASS = jac seeds
  every selectivity point + completes** (no multi-minute hang at any point).
- **C9 — Selectivity finishes on all 4 backends** at `fixed-total`. **PASS = a complete CSV per
  backend across all selectivity points.**

---

## 5. Commands — the runs

Same `--run-id` across a comparison set so seeded data is identical. `--selectivity-mode
fixed-total` everywhere. Deploy URL assumed `http://localhost:8080` (port-forward from deploy.sh).

```bash
# --- #1 full jac: default deploy, seed + warm ---
python harness.py --backend jac --url http://localhost:8080 \
    --run-id R1 --sweep fanout selectivity --selectivity-mode fixed-total

# --- #3 no-redis no-L1: SAME default deploy, reuse #1's seed, cold-L1 ---
python harness.py --backend jac --url http://localhost:8080 \
    --run-id R1 --skip-seed --cold-l1 --label jac_noredis_cold \
    --sweep fanout selectivity --selectivity-mode fixed-total

# --- #2 no-gti: GTI-off deploy (B5), own seed, warm ---
python harness.py --backend jac --url http://localhost:8080 \
    --run-id R1 --label jac_nogti --sweep fanout selectivity --selectivity-mode fixed-total

# --- #4 no-redis: no-redis deploy (B6, if achievable), own seed, warm ---
python harness.py --backend jac --url http://localhost:8080 \
    --run-id R1 --label jac_noredis --sweep fanout selectivity --selectivity-mode fixed-total

# --- baselines (for the same figures) ---
python harness.py --backend postgres   --url <pg_url>   --run-id R1 --selectivity-mode fixed-total
python harness.py --backend sqlalchemy --url <sqla_url> --run-id R1 --selectivity-mode fixed-total
python harness.py --backend neo4j      --url <neo4j_url> --run-id R1 --selectivity-mode fixed-total

# --- plot (reads results/*.csv → figures/) ---
python plot.py
```
**Order matters:** run **#1 (seed) before #3 (`--skip-seed`)** on the same deploy, else the
already-seeded guard fires. Each writes `results/<label>.csv`.

## 6. Report back (paste into your reply to the human)

For each task A1–A4, B5–B7, C8–C9: **PASS / FAIL / BLOCKED** + one line of evidence
(the command output, the latency number, the error). Then:
- `scp` results to the laptop: `scp 'clarity:~/DBaseRunner/results/*.csv' results/` and
  `figures/*.png` (results/figures are gitignored — do NOT commit them).
- Call out anything surprising (latency still high, writes lost, redis-disable impossible).

The human relays your report back to the coordinator session for memory + next steps.
