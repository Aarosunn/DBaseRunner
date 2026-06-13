# Fanout Run Playbook

SSH → committed code → `fig2_fanout.png`, with the **two jac gates** (`:pub` isolation + GTI on).
Do the first run manually; script it only after the sequence is proven.

**This run:**
- **Fanout only** — the selectivity sweep is still the filter-pushdown path mislabeled as
  type-selectivity (needs the FP→type refocus). Don't run it for a headline figure yet.
- Figure = **4 lines** (jac-GTI, postgres, sqla, neo4j). **No naive-jac line yet** — that needs
  the `JAC_TOPOLOGY_INDEX` toggle fix.
- Keep `--run-id run01` across **all 4** backends so their seeded data lines up.

---

## Phase 1 — push + get on cluster
1. **Laptop:** `git push origin main` (HTTPS + PAT). Verify: `git log --oneline origin/main -1`.
2. **SSH clarity** → `cd DBaseRunner` → `git pull`.

## Phase 2 — first fanout run (manual)
3. `rm -f results/*.csv`  — clear stale data first.
4. `./preflight.sh`  — kill strays, check ports 8001/8080, show pods.
5. `./build.sh postgres && ./build.sh neo4j`  — build image backends into minikube at `:v1`.
6. **Confirm on ONE backend first** (don't loop blind):
   ```bash
   LOCAL_PORT=8001 ./baselines.sh --backend postgres --run-id run01 -- --sweep fanout
   ```
   Check: deploys → runs → `response_bytes` grows with fanout → tears down clean.
7. **Then loop the other two:**
   ```bash
   for b in sqlalchemy neo4j; do
     LOCAL_PORT=8001 ./baselines.sh --backend "$b" --run-id run01 -- --sweep fanout
   done
   ```
8. **jac — deploy, pass BOTH gates, then run:**
   ```bash
   cd servers/jac && ./deploy.sh && cd ../..

   # ── GATE A: GTI on?  (index off => jac runs the slow naive path => invalid latency)
   # Runtime gate (topo_utils.impl.jac:_is_enabled): env JAC_INDEX_ENABLED overrides,
   # else falls back to jac.toml `topology_index`.
   grep topology_index servers/jac/jac.toml                      # expect: true  (the fallback)
   POD=$(kubectl get pod -l app=jaseci -o name | head -1)
   kubectl exec "$POD" -- printenv | grep -i JAC_INDEX_ENABLED   # EMPTY or true = ON; "false" = OFF (bad)

   # ── GATE B: :pub fix / isolation?  (fails => users collide => invalid data)
   JAC_BENCH_URL=http://localhost:8080 uv run pytest tests/test_jac_isolation.py -q   # must pass

   # both green -> run jac
   uv run python harness.py --backend jac --url http://localhost:8080 --run-id run01 --sweep fanout
   ```
9. **Plot:** `uv run python plot.py` → `figures/fig2_fanout.png`.
   - *Behavioral GTI check:* jac's curve should stay low and roughly flat. If it rises steeply
     like a naive scan, the index was OFF — Gate A missed something; don't trust jac's numbers.
10. **Retrieve:** `git add results/ figures/ && git commit && git push` → `git pull` on laptop to view.

## Phase 3 — script it (only after Phase 2 works)
- `jac_run.sh` = step 8 (deploy → Gate A → Gate B → harness).
- `run_all.sh` = preflight → build → baselines loop → `jac_run.sh` → plot.
- Commit + push.

## Phase 4 — every future run
```bash
git pull && RUN_ID=run02 ./run_all.sh    # run02/run03/… = a fresh label so data doesn't mix
```
`RUN_ID` is the env var the scripts read to tag a run; bump it per full run.

---

## If a gate fails
- **Gate B (isolation) red** → the `:pub` fix didn't take at runtime. Ship the 3-line figure
  (pg/sqla/neo4j are valid), debug jac before trusting any jac number.
- **Gate A (GTI off)** → jac is on the naive path; its latency is meaningless. Check
  `servers/jac/jac.toml` (`topology_index = true`) and that no env sets `JAC_INDEX_ENABLED=false`,
  redeploy, recheck. (The naive-jac ablation line is produced by deliberately setting
  `JAC_INDEX_ENABLED=false` on a separate jac deploy.)

## Port convention (don't unify)
- baseline backends → `LOCAL_PORT=8001`; jac → `8080` (jac-cloud default).
- Split on purpose: jac stays resident on 8080 (redeploying `--scale` is expensive) while the 3
  cycle through 8001. One shared port would collide.
