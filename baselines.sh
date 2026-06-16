#!/usr/bin/env bash
# Run the BASELINE backends (postgres, sqlalchemy, neo4j) on the clarity cluster.
# (Renamed from orchestrate.sh — it only does the 3 manifest backends, not jac.)
#
# Per backend: apply manifests -> wait for app rollout -> port-forward ->
# run harness.py (client-side timing) -> stop port-forward -> tear down.
# Sequential + teardown-between, so the Guaranteed-QoS pods never overcommit.
#
# Phase 5 needs the SAME --run-id across all backends, so one run_id is generated
# once here and passed to every harness invocation; pass the same to the jac run.
#
# jac is NOT handled here — it self-deploys via servers/jac/deploy.sh
# (jac start --scale); run it separately on port 8080 (--backend jac errors here).
#
# Usage:
#   ./baselines.sh                         # postgres, sqlalchemy, neo4j; default sweeps
#   ./baselines.sh --backend postgres      # one backend
#   ./baselines.sh --keep                  # don't tear down pods after each run
#   ./baselines.sh --run-id r42 -- --sweep fanout --trials 30
#                                            # everything after `--` is passed to harness.py
#
# Env: NAMESPACE (default: default), LOCAL_PORT (default: 8000),
#      ROLLOUT_TIMEOUT (default: 300s), HEALTH_RETRIES (default: 60).
set -euo pipefail

cd "$(dirname "$0")"

# ── backend -> app Service/Deployment name (both share the name in these manifests)
# jac is intentionally absent: it deploys via servers/jac/deploy.sh, not k8s/<b>/.
declare -A APP=(
  [postgres]=postgres-app
  [sqlalchemy]=dbaserunner-sqlalchemy
  [neo4j]=neo4j-app
)
ORDER=(postgres sqlalchemy neo4j)

NAMESPACE="${NAMESPACE:-default}"
LOCAL_PORT="${LOCAL_PORT:-8000}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
HEALTH_RETRIES="${HEALTH_RETRIES:-300}"  # seconds; neo4j-db cold-boot (the bottleneck) can run
                                         # well past 2min on a loaded single-node minikube

# ── args
SELECTED="all"
RUN_ID=""
SEED_DIR="seed/"
KEEP=0
HARNESS_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)  SELECTED="$2"; shift 2 ;;
    --run-id)   RUN_ID="$2"; shift 2 ;;
    --seed-dir) SEED_DIR="$2"; shift 2 ;;
    --keep)     KEEP=1; shift ;;
    --)         shift; HARNESS_ARGS=("$@"); break ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date +%Y%m%d%H%M%S)"; fi

if [[ "$SELECTED" == "jac" ]]; then
  echo "jac is not orchestrated here — it self-deploys via jac-cloud --scale." >&2
  echo "  cd servers/jac && ./deploy.sh" >&2
  echo "  then: uv run python harness.py --backend jac --url http://localhost:8080 --run-id <id>" >&2
  exit 2
fi

if [[ "$SELECTED" == "all" ]]; then BACKENDS=("${ORDER[@]}"); else BACKENDS=("$SELECTED"); fi

# uv runs the harness in the managed venv; fall back to plain python if absent.
RUNNER=(uv run python); command -v uv >/dev/null 2>&1 || RUNNER=(python3)

PF_PID=""
stop_pf() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; PF_PID=""; }
trap stop_pf EXIT

wait_for_port() {
  local port="$1" i
  for ((i=0; i<HEALTH_RETRIES; i++)); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then exec 3>&- 3<&-; return 0; fi
    sleep 1
  done
  return 1
}

# A TCP socket opens the instant `kubectl port-forward` binds — long before the app
# accepts requests (FastAPI won't serve until lifespan startup completes, which here
# blocks on the DB becoming reachable + schema bootstrap; neo4j adds JVM cold-boot).
# So gate on an actual HTTP 200 from /health, not just the open port.
#
# HEALTH_RETRIES is a wall-clock SECONDS budget. Each curl is capped at 5s
# (--max-time): while the app is startup-blocked its socket is OPEN but unresponsive,
# so an un-capped curl would HANG on one request (looks like a frozen [3.5/5]) instead
# of polling. With -m 5 we poll, and emit a heartbeat every ~15s so a slow cold start
# (a fresh DB running initdb / a cold JVM, ~60-120s) visibly shows progress.
wait_for_health() {
  local port="$1" code last_beat=0 start=$SECONDS
  while (( SECONDS - start < HEALTH_RETRIES )); do
    code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    [[ "$code" == "200" ]] && return 0
    if (( SECONDS - last_beat >= 15 )); then
      echo "      /health not ready: $((SECONDS - start))s/${HEALTH_RETRIES}s, last=${code:-000} (cold DB init ~60-120s)" >&2
      last_beat=$SECONDS
    fi
    sleep 1
  done
  return 1
}

echo "==> run_id=${RUN_ID}  namespace=${NAMESPACE}  backends=${BACKENDS[*]}"

for b in "${BACKENDS[@]}"; do
  svc="${APP[$b]:-}"
  if [[ -z "$svc" ]]; then echo "unknown backend: $b" >&2; exit 2; fi
  echo ""
  echo "════════════════ ${b} ════════════════"

  # No-build sqlalchemy ships source via a ConfigMap populated from the CURRENT
  # repo files; image-based backends (postgres, neo4j) no-op.
  if [[ "$b" == "sqlalchemy" ]]; then
    echo "  [1/5] populate src ConfigMap from current source"
    NAMESPACE="$NAMESPACE" ./k8s-configmap.sh "$b"
  fi

  echo "  [1/5] kubectl apply -f k8s/${b}/"
  kubectl apply -n "$NAMESPACE" -f "k8s/${b}/"

  echo "  [2/5] wait for rollout of deployment/${svc}"
  kubectl rollout status -n "$NAMESPACE" "deployment/${svc}" --timeout="$ROLLOUT_TIMEOUT"

  echo "  [3/5] port-forward svc/${svc} ${LOCAL_PORT}->8000"
  kubectl port-forward -n "$NAMESPACE" "svc/${svc}" "${LOCAL_PORT}:8000" >/dev/null 2>&1 &
  PF_PID=$!
  if ! wait_for_port "$LOCAL_PORT"; then
    echo "  ERROR: port ${LOCAL_PORT} never opened for ${b}" >&2
    stop_pf; [[ "$KEEP" -eq 0 ]] && kubectl delete -n "$NAMESPACE" -f "k8s/${b}/" --ignore-not-found
    exit 1
  fi
  echo "  [3.5/5] wait for ${b} /health to return 200"
  if ! wait_for_health "$LOCAL_PORT"; then
    echo "  ERROR: ${b} /health not 200 after ${HEALTH_RETRIES}s (app/db startup not ready)" >&2
    stop_pf; [[ "$KEEP" -eq 0 ]] && kubectl delete -n "$NAMESPACE" -f "k8s/${b}/" --ignore-not-found
    exit 1
  fi

  echo "  [4/5] harness.py --backend ${b} --run-id ${RUN_ID}"
  "${RUNNER[@]}" harness.py \
    --backend "$b" \
    --url "http://localhost:${LOCAL_PORT}" \
    --run-id "$RUN_ID" \
    --seed-dir "$SEED_DIR" \
    "${HARNESS_ARGS[@]}"

  stop_pf

  if [[ "$KEEP" -eq 0 ]]; then
    echo "  [5/5] teardown k8s/${b}/"
    kubectl delete -n "$NAMESPACE" -f "k8s/${b}/" --ignore-not-found
  else
    echo "  [5/5] --keep set; leaving ${b} running"
  fi
done

echo ""
echo "==> done. results in results/  (run_id=${RUN_ID})"
echo "    plot with:  uv run python plot.py --results results/ --figures figures/"
