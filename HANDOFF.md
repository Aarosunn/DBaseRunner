# HANDOFF — cluster (clarity) test session

Goal of this session: take the Phase-4 single-hop harness from "correct by construction +
unit-tested" to "runs end-to-end on the cluster," by clearing the cluster-only blockers.
Local work is done (111 harness + 26 server tests green). **Nothing has run live yet.**

> **Key idea — the harness self-verifies.** Every sweep point does
> `ensure_user → seed → verify_seed → time`. `verify_seed` hard-fails (SystemExit) with a diff
> if the seed count, the `like_count > 10` predicate, or cross-backend identity is wrong. So a
> `harness.py` run that **completes without SystemExit** has *proven* seed + predicate + auth
> for that backend. You mostly test blockers by running it and reading the error.

---

## 0. Get the code onto clarity (do this first)

The deploy fixes are in git. Make sure they're pushed from your laptop, then pull on clarity.

```bash
# laptop
git -C /home/aaron/Dev/Research/JacDB/DBaseRunner push

# clarity
git clone git@github.com:Aarosunn/DBaseRunner.git   # or: git -C DBaseRunner pull
cd DBaseRunner
uv sync          # harness venv (requests, pytest); needs uv installed
uv run pytest -q # sanity: 111 passing
```

Confirm tooling on clarity: `kubectl version --client`, `docker version`, `uv --version`,
`kubectl config current-context` (must point at the clarity cluster).

---

## 1. Pre-deploy (one-time per code change)

| Backend | Make the cluster run the latest code |
|---|---|
| postgres | `docker build -t dbaserunner/postgres-app:latest servers/postgres && docker push dbaserunner/postgres-app:latest` |
| neo4j | `docker build -t dbaserunner/neo4j-app:latest servers/neo4j && docker push dbaserunner/neo4j-app:latest` |
| jac / sqlalchemy | nothing — `orchestrate.sh` runs `k8s-configmap.sh` from current source at deploy |

> Registry: the manifests reference `dbaserunner/<b>-app:latest`. If clarity can't pull from
> Docker Hub under that name, retag/push to the cluster's registry and update `image:` in
> `k8s/postgres/deployment.yaml` + `k8s/neo4j/deployment.yaml`.

**Fresh schema** — `load_own_tweets` needs the new `like_count` column, and `CREATE TABLE IF
NOT EXISTS` won't add it to an existing DB. Each backend's manifest brings its own DB pod, so a
clean deploy starts fresh. If you reuse a DB, pass `--reset` once or `ALTER TABLE tweets ADD
COLUMN like_count BIGINT NOT NULL DEFAULT 0;`.

---

## 2. Bring-up order — easy backends first, jac LAST

Prove the harness mechanics on a predictable backend before touching the uncertain one.
**Recommended order: postgres → sqlalchemy → neo4j → jac.**

Smoke one backend with a tiny run (fast fail; seeding is the slow/risky part):

```bash
./orchestrate.sh --backend postgres --run-id smoke -- --sweep fanout --warmup 2 --trials 2
```

What `orchestrate.sh` does per backend: populate ConfigMap (jac/sqla) → `kubectl apply` → wait
rollout → port-forward 8000 → run `harness.py` → teardown. A clean finish writes
`results/postgres.csv` + `results/postgres_meta.json`.

Manual equivalent (more control while debugging):
```bash
kubectl apply -f k8s/postgres/
kubectl rollout status deployment/postgres-app --timeout=300s
kubectl port-forward svc/postgres-app 8000:8000 &
uv run python harness.py --backend postgres --url http://localhost:8000 \
    --run-id smoke --sweep fanout --warmup 2 --trials 2
```

---

## 3. Cluster-blocker checklist

Work top to bottom. Each row: how to test, pass signal, fix if it fails.

### B1 — Deploy + health (all backends)
- **Test:** `kubectl get pods`; harness prints no health error.
- **Pass:** app pod `Running`/`Ready`; harness proceeds past startup.
- **Fail:** `"<b> health check failed"` → `kubectl get pods`, `kubectl logs deploy/<svc>`,
  `kubectl logs deploy/<svc> -c <init>` for initContainer (jac/sqla). Common: image pull error,
  initContainer pip/clone failure, DB not ready.

### B2 — Seed + predicate + identity (all backends) — the big one, auto-checked
- **Test:** just run the harness. `verify_seed` runs after each point's seed.
- **Pass:** harness completes; CSV has rows for every param point.
- **Fail — read the SystemExit:**
  - `expected N matching tweets, got 0` → predicate filtered everything **or** seed didn't land
    **or** (jac) the walker didn't bind your root. Check the server: did `seed_tweets` run? Is
    `like_count` stored? Is `load_own_tweets` applying `> 10`?
  - `got M` (M ≠ expected, M > 0) → predicate not applied server-side (returning all tweets) or
    wrong `like_count` band seeded.
  - `like_count L is not > threshold` → server returned a sub-threshold tweet → predicate not
    filtering.
  - `key set mismatch` → wrong tweets returned → identity/root-binding issue (esp. jac).

### B3 — Postgres index-only scan (optional, perf-quality)
- **Test:** `kubectl exec` into the PG pod and
  `EXPLAIN (ANALYZE) SELECT ... WHERE author_id=$1 AND like_count>10` (mirror
  `routes/walker.py` SQL).
- **Pass:** `Index Only Scan using idx_tweets_author_created`. If it hits the heap, the covering
  index `INCLUDE (content, like_count)` may not have applied (existing index not rebuilt → fresh
  DB or drop+recreate the index).

### B4 — jac deploy mechanism ⚠ (the unresolved one)
`k8s/jac/deployment.yaml` runs `jac start --scale` *inside* a pod. jac-cloud's `--scale` normally
**self-deploys** to k8s (service `jaseci-service`), which is what `servers/jac/deploy.sh` does.
- **Test path A (manifest):** `./orchestrate.sh --backend jac --run-id smoke -- --sweep fanout
  --warmup 1 --trials 1`. Watch `kubectl get pods` + `kubectl logs deploy/dbaserunner-jac`.
- **If the pod crashes / `--scale` tries to spawn its own pods / never gets Ready → use path B.**
- **Test path B (native):** `bash servers/jac/deploy.sh` (deploys via jac-cloud, service
  `jaseci-service`), then run the harness by hand pointing at the native service:
  ```bash
  kubectl port-forward svc/jaseci-service 8000:8000 &
  uv run python harness.py --backend jac --url http://localhost:8000 \
      --run-id smoke --sweep fanout --warmup 1 --trials 1
  ```
  If B works and A doesn't: update `k8s/jac/` (or `orchestrate.sh`'s jac path) to match the
  native deploy, and note it. Ping me to reconcile.

### B5 — GTI actually ON (jac) — validity-critical
The whole jac claim depends on the **filter_pushdown branch build with the index enabled**. The
PyPI build makes the flag a no-op.
- **Test:** `kubectl exec deploy/dbaserunner-jac -- printenv JAC_TOPOLOGY_INDEX` → `true`.
  Confirm the initContainer installed from `cse584-W26/jaseci@filter_pushdown` (it's in the
  install command; check `kubectl logs ... -c install-jac`).
- **Deeper (optional):** the server has a `bench_single_hop` walker that reports L1 sizes with
  index on/off — the paper's signature was `on_l1≈5` vs `off_l1≈103`. Hitting it both ways
  confirms GTI is doing real work, not a no-op.

### B6 — jac auth quirks
- **Test:** harness `ensure_user` registers `bench_<run_id>_fanout_100` (a non-email username).
- **Fail:** if jac-cloud rejects non-email registration → `ensure_user` errors. Fix: have the jac
  adapter send `email="<username>@bench.local"` consistently in register+login (small adapter
  change — ping me). Also confirm calling a `:pub` walker *with* a JWT executes against that
  user's root (B2 key-set check already covers this).

---

## 4. Full headline run (after all four pass smoke)

Use **one shared run-id across all four** (Phase 5 needs it):

```bash
RID=run_$(date +%Y%m%d_%H%M)
./orchestrate.sh --run-id "$RID"          # all 4, default sweeps (fanout + selectivity)
uv run python plot.py --results results/ --figures figures/
```
Outputs `results/{backend}.csv` + `{backend}_meta.json`, and
`figures/fig5_fanout.png`, `fig6_selectivity.png` (+ `*_bytes.png`).

**Sanity-read before trusting anything:**
- `*_bytes.png` should **rise** with selectivity. A **flat** line = the sweep degenerated to
  identical data (the original no-op-sweep bug resurfacing).
- Per-backend latency should vary across param points (not a flat line).
- `cold_l1` in each `_meta.json` should be `false` for headline runs (don't pass `--cold-l1`).

---

## 5. What a green run does and does NOT mean

✅ Means: the harness, seed, predicate, identity, and 4-backend control plane all work
end-to-end; you have real single-hop latency for `load_own_tweets`.

❌ Does **not** mean you've reproduced the paper. Two gaps remain (future phases, not this
session):
- **Phase 4.5** — the paper's Fig 5/6 use the **2-hop `load_feed`** follow-graph (fan-out =
  followees). This harness is **single-hop own-tweets** → expect *parity*, not the paper's
  separation. The Jac-favoring workload isn't built.
- **Phase 5** — no cross-backend correctness comparison yet; `verify_seed` checks each backend
  against its own spec, not against the others.

---

## 6. Quick reference

```bash
./orchestrate.sh                              # all backends, default sweeps
./orchestrate.sh --backend <b>                # one backend
./orchestrate.sh --keep -- --sweep fanout     # keep pods up; pass args to harness
./k8s-configmap.sh <jac|sqlalchemy>           # (re)populate src ConfigMap from current source
uv run python harness.py --help               # flags: --run-id --seed-dir --skip-seed --reset --cold-l1
uv run python plot.py --results results/ --figures figures/
```

Services (all port 8000): `dbaserunner-jac` (or native `jaseci-service`), `postgres-app`,
`dbaserunner-sqlalchemy`, `neo4j-app`.

When something needs a code change rather than a config tweak (jac email shim, jac deploy
reconciliation, predicate fix), capture the exact SystemExit / `kubectl logs` output and hand it
back — those are small, targeted local fixes.
