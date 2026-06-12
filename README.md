# DBaseRunner — JacDB benchmark harness

Unified benchmark harness for the paper *"The Database Is the Language"* (Figures 5/6).
One client-side harness times the **same question** (`load_own_tweets`, predicate
`like_count > 10`) against four backends — **jac** (jac-cloud + GTI/Filter-Pushdown),
**hand-tuned postgres**, **sqlalchemy** (ORM), **neo4j** — each answering in its own
native idiom. A sweep point is a *dataset*, not a request parameter: every point seeds a
fresh eval user with deterministic data, verifies it, then times an empty POST under that
user's auth. Design specs live in `docs/specs/`.

> **Scope note (read this).** The harness currently benchmarks the **single-hop**
> `load_own_tweets` workload. The paper's Fig 5/6 use the **2-hop `load_feed`** follow-graph
> (fan-out = followees), which is **Phase 4.5 and not yet built**. Single-hop validates the
> whole pipeline but is expected to show parity, not the paper's separation. See
> [Known gaps](#known-gaps).

---

## Repo layout

```
DBaseRunner/
  harness.py            # client-side perf_counter timing + seed/verify flow + CSV
  seed_gen.py           # deterministic seed generator -> seed/*.json
  plot.py               # results/*.csv -> figures/ (Fig5/Fig6 + bytes sanity)
  orchestrate.sh        # deploy -> wait -> port-forward -> harness -> teardown (per backend)
  backends/             # control-plane adapters (auth, seed, load_own_tweets, health)
  seed/                 # committed deterministic seed specs (10 points + manifest)
  servers/{jac,postgres,sqlalchemy,neo4j}/   # the 4 backend services (deployed separately)
  k8s/{jac,postgres,sqlalchemy,neo4j}/       # deployment manifests
  results/              # per-trial CSVs land here (one per backend) + *_meta.json
  figures/              # plot.py output
  docs/specs/           # harness-fix-spec.md, seed-design-spec.md
```

---

## 1. Local setup + tests (no cluster needed)

Dependencies are managed by [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create .venv from pyproject.toml + uv.lock
uv run pytest           # 111 harness/seed/adapter/plot tests
uv run ruff check .     # lint
```

The four servers have their **own** deps/venvs (they deploy as separate containers). Their
unit tests run independently and need no live DB (mocked / in-memory sqlite):

```bash
for s in postgres sqlalchemy neo4j; do (cd servers/$s && .venv/bin/python -m pytest tests/ -q); done
```

## 2. Generate seed data (deterministic, already committed)

```bash
uv run python seed_gen.py --out seed/      # 10 point files + manifest.json, byte-identical
```
Only regenerate if you bump `SPEC_VERSION` in `seed_gen.py`.

---

## 3. Reproduce on the cluster (clarity)

Everything above is local. The four servers run on k8s. **Your harness/server edits do not
reach the cluster until you rebuild the images / regenerate the ConfigMaps below.**

### 3.0 Prereqs
- `kubectl` pointed at the clarity cluster (`kubectl config current-context`).
- `docker` + a registry you can push to (for the two image-based backends).
- `uv` on the machine running the harness (it port-forwards to the services).

### 3.1 Get each backend's code onto the cluster

All backends use the uniform `dbaserunner-*` k8s manifests under `k8s/<backend>/`. Two ship
code as a prebuilt image; two (no-build) ship it via a source ConfigMap populated from the
current repo files by `./k8s-configmap.sh <backend>`.

| Backend | How code is delivered | Update after editing server code |
|---|---|---|
| **postgres** | custom app image `dbaserunner/postgres-app:vN` | build into minikube with a **new tag** — see the minikube box below |
| **neo4j** | custom app image `dbaserunner/neo4j-app:vN` | build into minikube with a **new tag** — see the minikube box below |
| **jac** | `dbaserunner-jac-src` **ConfigMap** (main.jac + server.jac + jac.toml) | `./k8s-configmap.sh jac` |
| **sqlalchemy** | `dbaserunner-sqlalchemy-src` **ConfigMap** | `./k8s-configmap.sh sqlalchemy` |

`orchestrate.sh` runs `k8s-configmap.sh` automatically for jac/sqlalchemy before `apply`. The
ConfigMap step is per-backend, so deploying one backend never touches another.

> The Phase-4 changes (`like_count`, `seed_tweets`, the `like_count > 10` predicate) are in
> these files — they are NOT in the running pods until you rebuild the image / re-run
> `k8s-configmap.sh`.

> **⚠ minikube image gotchas (these cost hours — read before building).**
> minikube uses `imagePullPolicy: Never`, so it serves whatever local image the tag points at.
> 1. **Never reuse a tag.** Re-loading `dbaserunner/postgres-app:latest` does NOT replace the
>    running layer — the pod keeps serving the old sha. Bump the tag every build (`:v2`, `:v3`, …)
>    and update the manifest to match. No `docker push` / registry is involved.
> 2. **Build straight into minikube**, which skips the flaky `minikube image load`:
>    ```bash
>    eval $(minikube docker-env)
>    docker build -t dbaserunner/postgres-app:v3 servers/postgres
>    eval $(minikube docker-env -u)
>    sed -i 's|dbaserunner/postgres-app:[^"[:space:]]*|dbaserunner/postgres-app:v3|' k8s/postgres/*.yaml
>    ```
> 3. **Confirm the pod is on the new tag** before trusting it:
>    `kubectl get pod -o jsonpath='{.items[*].spec.containers[0].image}{"\n"}' | grep postgres-app`

> **⚠ jac deploy is unverified.** `k8s/jac/deployment.yaml` runs `jac start --scale` *inside* a
> pod. jac-cloud's `--scale` mode normally self-deploys to k8s (that's what `servers/jac/deploy.sh`
> does natively → service `jaseci-service`). Whether the in-pod manifest works, or whether you
> must use the native `servers/jac/deploy.sh` path instead (and point the harness at
> `svc/jaseci-service`), is the one deploy question only a live cluster can settle. `deploy.sh`
> is kept as the fallback.

### 3.2 Start from a fresh schema

`load_own_tweets` now reads a `like_count` column/field. Postgres `schema.sql` uses
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`, so **a pre-existing DB will
NOT pick up the new column or the extended covering index**. Either:
- start from a fresh DB pod (the manifests create one), or
- run `harness.py --reset` once (wipes PG/SQLA/neo4j via `clear_data`; jac no-op), or
- hand-apply `ALTER TABLE tweets ADD COLUMN like_count BIGINT NOT NULL DEFAULT 0;`.

### 3.3 Run

One backend, manually:
```bash
kubectl apply -f k8s/postgres/
kubectl rollout status deployment/postgres-app

# Use a FREE local port and bind IPv4 explicitly. If port-forward prints only
# "[::1]:PORT" (no 127.0.0.1), that local port is already taken — pick another.
pkill -9 -f "kubectl port-forward"; ss -tlnp | grep :8001    # 8001 must be free (no output)
kubectl port-forward --address 127.0.0.1 svc/postgres-app 8001:8000 >/tmp/pf.log 2>&1 &
sleep 2; cat /tmp/pf.log                                     # must say 127.0.0.1:8001 -> 8000

# Sanity-check you're on the right app BEFORE running the harness:
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8001/walker/load_own_tweets
#   401 = correct app, route exists, needs auth (the harness logs in) -> good
#   405 = WRONG app on this port (e.g. a stray `jac serve`); find it: ss -tlnp | grep :8001

uv run python harness.py --backend postgres --url http://localhost:8001 \
    --run-id run01 --sweep fanout selectivity
```
> Sanity-check the result CSV: `response_bytes` should grow with `param_value` on the fanout
> sweep (more tweets seeded → bigger payload). Flat or zero bytes means the seed did not land.

All four, automated (apply → wait → port-forward → harness → teardown per backend):
```bash
./orchestrate.sh --run-id run01                  # all backends, default sweeps
./orchestrate.sh --backend jac                   # one backend
./orchestrate.sh --keep -- --sweep fanout        # keep pods up; pass --sweep to harness
```
`orchestrate.sh` generates one `run_id` and passes it to every backend. Env knobs:
`NAMESPACE`, `LOCAL_PORT`, `ROLLOUT_TIMEOUT`, `HEALTH_RETRIES`.

> **Phase 5 requires the SAME `--run-id` across all four backend runs** so the seeded
> usernames/data line up for cross-system comparison. `orchestrate.sh` enforces this; if you
> run `harness.py` by hand, pass the same `--run-id` every time.

### 3.4 Plot

```bash
uv run python plot.py --results results/ --figures figures/
# figures/fig5_fanout.png, fig6_selectivity.png  (+ *_bytes.png sanity plots)
```
`*_bytes.png` plots `response_bytes` vs param — it should rise with selectivity; a **flat**
line means the sweep degenerated to identical data (the original no-op-sweep bug).

---

## Cache modes

All-warm by default (20-request warmup conditions L1 like the baselines' buffer pools; no
clear between trials). `--cold-l1` is an **opt-in jac-only diagnostic** that clears L1 before
each trial; it's recorded as `"cold_l1": true` in `results/{backend}_meta.json` so those rows
can never be mistaken for the headline figures. Don't use it for headline runs.

## Iteration loop

- Editing `harness.py` / `backends/` / `seed_gen.py` (the **client**) → just re-run. No redeploy.
- Editing **server** code → postgres/neo4j need image rebuild+push; jac/sqlalchemy need a
  ConfigMap regen (the deploy scripts in §3.1).

---

## Known gaps

| Gap | Status |
|---|---|
| **Phase 4.5 — 2-hop `load_feed`** (the paper's actual Fig 5/6 workload: fan-out = followees) | **not built.** Current harness is single-hop `load_own_tweets` → expect parity, not the paper's separation. |
| **Phase 5 — cross-backend correctness** (compare result sets across backends) | not built. `verify_seed` checks each backend against its own spec, not against the others. |
| **Live verification** | nothing has run on a cluster. All code is correct-by-construction + unit-tested. First run must confirm: jac `:pub seed_tweets` + JWT binds the caller's root; detached liker Profiles persist; per-point `verify_seed` passes; jac-cloud accepts non-email usernames. |

## Tests at a glance

```bash
uv run pytest               # harness + seed_gen + adapters + plot (111)
uv run ruff check .         # lint
(cd servers/postgres   && .venv/bin/python -m pytest tests/ -q)   # 4
(cd servers/sqlalchemy && .venv/bin/python -m pytest tests/ -q)   # 8
(cd servers/neo4j      && .venv/bin/python -m pytest tests/ -q)   # 14
```
