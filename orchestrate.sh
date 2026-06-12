#!/usr/bin/env bash
# Orchestrate a full benchmark run on the clarity k8s cluster.
#
# Per backend: apply manifests -> wait for app rollout -> port-forward ->
# run harness.py (client-side timing) -> stop port-forward -> tear down.
#
# Phase 5 needs the SAME --run-id across all four backends, so a single run_id
# is generated once here and passed to every harness invocation.
#
# Usage:
#   ./orchestrate.sh                         # all 4 backends, default sweeps
#   ./orchestrate.sh --backend jac           # one backend
#   ./orchestrate.sh --keep                  # don't tear down pods after each run
#   ./orchestrate.sh --run-id r42 -- --sweep fanout --trials 30
#                                            # everything after `--` is passed to harness.py
#
# Env: NAMESPACE (default: default), LOCAL_PORT (default: 8000),
#      ROLLOUT_TIMEOUT (default: 300s), HEALTH_RETRIES (default: 60).
set -euo pipefail

cd "$(dirname "$0")"

# ── backend -> app Service/Deployment name (both share the name in these manifests)
declare -A APP=(
  [jac]=dbaserunner-jac
  [postgres]=postgres-app
  [sqlalchemy]=dbaserunner-sqlalchemy
  [neo4j]=neo4j-app
)
ORDER=(jac postgres sqlalchemy neo4j)

NAMESPACE="${NAMESPACE:-default}"
LOCAL_PORT="${LOCAL_PORT:-8000}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
HEALTH_RETRIES="${HEALTH_RETRIES:-60}"

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

echo "==> run_id=${RUN_ID}  namespace=${NAMESPACE}  backends=${BACKENDS[*]}"

for b in "${BACKENDS[@]}"; do
  svc="${APP[$b]:-}"
  if [[ -z "$svc" ]]; then echo "unknown backend: $b" >&2; exit 2; fi
  echo ""
  echo "════════════════ ${b} ════════════════"

  # No-build backends (jac, sqlalchemy) ship source via a ConfigMap populated
  # from the CURRENT repo files; image-based backends (postgres, neo4j) no-op.
  if [[ "$b" == "jac" || "$b" == "sqlalchemy" ]]; then
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
