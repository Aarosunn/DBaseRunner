#!/usr/bin/env bash
#
# Tear down the littleX_sqlalchemy K8s deployment created by
# deploy_sqlalchemy_k8s.sh and stop the local port-forward. Idempotent.
#
# Style mirrors littleX_FULLSTACK/teardown.sh and
# littleX-benchmarks/Neo4j/teardown_neo4j_k8s.sh, but scoped to the
# littleX-sqlalchemy resources only — we do NOT nuke the whole namespace
# because jac-scale or Neo4j may also be running there.
#
# Usage:
#   ./teardown_sqlalchemy_k8s.sh

set -euo pipefail

NAMESPACE="default"
APP_LABEL="app=littlex-sqlalchemy"
LOCAL_HTTP_PORT=8080
CONFIGMAP_NAME="littlex-sqlalchemy-src"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/k8s/manifest.yaml"

echo "Tearing down littleX_sqlalchemy deployment..."

if [ -f "$MANIFEST" ]; then
    kubectl delete -n "$NAMESPACE" -f "$MANIFEST" --ignore-not-found 2>/dev/null \
        && echo "  Deleted Postgres + Flask resources" \
        || echo "  Nothing to delete"
else
    kubectl delete deployment,statefulset,svc -n "$NAMESPACE" -l "$APP_LABEL" --ignore-not-found 2>/dev/null \
        && echo "  Deleted resources by label" \
        || echo "  Nothing to delete"
fi

# The src ConfigMap is created out-of-band by deploy_sqlalchemy_k8s.sh, so
# the manifest delete above does NOT remove it. Drop it explicitly.
kubectl delete configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --ignore-not-found 2>/dev/null \
    && echo "  Deleted source ConfigMap '$CONFIGMAP_NAME'" \
    || true

# Kill any active port-forward on the http port (matches FULLSTACK/teardown.sh style)
if lsof -ti:${LOCAL_HTTP_PORT} &>/dev/null; then
    kill $(lsof -ti:${LOCAL_HTTP_PORT}) 2>/dev/null \
        && echo "  Killed port-forward on :${LOCAL_HTTP_PORT}"
fi

echo ""
echo "Teardown complete."
