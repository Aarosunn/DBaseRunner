#!/usr/bin/env bash
#
# Deploy littleX_sqlalchemy (Flask + Postgres) into the same Kubernetes
# namespace where jac-scale runs, then start a port-forward on localhost:8080
# so the benchmark scripts in littleX-benchmarks/ can hit it via --url.
#
# This is the "Option 1" fairness setup: every backend (Jac, Neo4j,
# SQLAlchemy) takes the same path
#   bench host -> kubectl port-forward -> ClusterIP svc -> pod
# so latency differences reflect the backend, not the deployment topology.
#
# This script does NOT build a Docker image. The Flask source is shipped
# into the cluster as a ConfigMap and an initContainer pip-installs the
# requirements at startup using the stock python:3.12-slim image. To iterate
# on the source you just re-run this script — it deletes and recreates the
# ConfigMap and rolls the Flask deployment.
#
# Style mirrors littleX_FULLSTACK/deploy.sh and
# littleX-benchmarks/Neo4j/deploy_neo4j_k8s.sh — same NAMESPACE convention,
# same wait-for-pod pattern, same port-forward-in-background pattern.
#
# Usage:
#   ./deploy_sqlalchemy_k8s.sh

set -euo pipefail

NAMESPACE="default"
APP_LABEL="app=littlex-sqlalchemy"
TIMEOUT=300
LOCAL_HTTP_PORT=8080
CONFIGMAP_NAME="littlex-sqlalchemy-src"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/k8s/manifest.yaml"

echo "Checking for existing littleX_sqlalchemy deployment..."
EXISTING=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | wc -l)
if [ "$EXISTING" -gt 0 ]; then
    echo "littleX_sqlalchemy deployment already running:"
    kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL"
    echo "Run ./teardown_sqlalchemy_k8s.sh first if you want a fresh deployment."
    exit 1
fi

# ---------------------------------------------------------------------------
# Create the source ConfigMap. We rename each file as we go so the keys are
# flat (ConfigMap keys cannot contain "/") but get mounted into the right
# directory tree by the manifest's `items: ... path:` mappings.
# ---------------------------------------------------------------------------
echo "Creating source ConfigMap '$CONFIGMAP_NAME' from local files..."
kubectl delete configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1
kubectl create configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" \
    --from-file=requirements.txt="$SCRIPT_DIR/requirements.txt" \
    --from-file=src__init__.py="$SCRIPT_DIR/src/__init__.py" \
    --from-file=src_models.py="$SCRIPT_DIR/src/models.py" \
    --from-file=src_routes_user.py="$SCRIPT_DIR/src/routes/user.py" \
    --from-file=src_routes_walker.py="$SCRIPT_DIR/src/routes/walker.py"

echo "Applying $MANIFEST to namespace '$NAMESPACE'..."
kubectl apply -n "$NAMESPACE" -f "$MANIFEST"

echo "Waiting for postgres pod to become Ready..."
if ! kubectl wait pod -n "$NAMESPACE" \
        -l "$APP_LABEL,component=postgres" \
        --for=condition=Ready --timeout="${TIMEOUT}s" 2>/dev/null; then
    echo "Postgres pod did not become Ready within ${TIMEOUT}s. Logs:" >&2
    kubectl logs -n "$NAMESPACE" -l "$APP_LABEL,component=postgres" --tail=50 || true
    exit 1
fi

echo "Waiting for flask pod to become Ready (initContainer pip-install can take ~30s)..."
if ! kubectl wait pod -n "$NAMESPACE" \
        -l "$APP_LABEL,component=flask" \
        --for=condition=Ready --timeout="${TIMEOUT}s" 2>/dev/null; then
    echo "Flask pod did not become Ready within ${TIMEOUT}s. Logs:" >&2
    echo "--- pip-install initContainer:" >&2
    kubectl logs -n "$NAMESPACE" -l "$APP_LABEL,component=flask" -c pip-install --tail=80 || true
    echo "--- flask container:" >&2
    kubectl logs -n "$NAMESPACE" -l "$APP_LABEL,component=flask" -c flask --tail=80 || true
    exit 1
fi

echo ""
echo "Deployment ready!"
kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL"
echo ""
echo "Setting up port-forward on localhost:${LOCAL_HTTP_PORT}..."

# Free the port if something stale is on it (matches FULLSTACK/teardown.sh style)
if lsof -ti:${LOCAL_HTTP_PORT} &>/dev/null; then
    echo "  Killing stale process on :${LOCAL_HTTP_PORT}"
    kill $(lsof -ti:${LOCAL_HTTP_PORT}) 2>/dev/null || true
    sleep 1
fi

kubectl port-forward svc/littlex-sqlalchemy ${LOCAL_HTTP_PORT}:8000 -n "$NAMESPACE" &
sleep 2

if lsof -ti:${LOCAL_HTTP_PORT} &>/dev/null; then
    echo "Flask is reachable at http://localhost:${LOCAL_HTTP_PORT}"
    echo ""
    echo "Network path (matches jac-scale):"
    echo "  bench host -> kubectl port-forward -> svc/littlex-sqlalchemy -> flask pod"
    echo "                                                                  |"
    echo "                                                                  v"
    echo "                                                          svc/littlex-postgres -> postgres pod"
    echo ""
    echo "Run benchmarks against http://localhost:${LOCAL_HTTP_PORT}, e.g.:"
    echo "  cd ../littleX-benchmarks"
    echo "  python bench_filter_pushdown.py -u http://localhost:${LOCAL_HTTP_PORT} -r 10"
    echo "  python bench_evaluation.py      -u http://localhost:${LOCAL_HTTP_PORT} -r 10"
else
    echo "Port-forward up but :${LOCAL_HTTP_PORT} not yet bound — give it a few more seconds."
fi
