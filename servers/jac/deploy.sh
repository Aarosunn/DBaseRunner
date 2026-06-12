#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="default"
APP_LABEL="app=jaseci"
TIMEOUT=180  # seconds to wait for pod readiness
LITTLEX_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Checking for existing deployment..."
EXISTING=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | wc -l)
if [ "$EXISTING" -gt 0 ]; then
    echo "Deployment already running:"
    kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL"
    echo "Run ./teardown.sh first if you want a fresh deployment."
    exit 1
fi

echo "No existing deployment found. Starting littleX (backend only)..."
cd "$LITTLEX_DIR"
jac start main.jac --no-dev --no_client --scale --experimental &
JAC_PID=$!

echo "Waiting for jaseci pod to appear..."
ELAPSED=0
until kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | grep -q .; do
    sleep 3
    ELAPSED=$((ELAPSED + 3))
    if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
        echo "Timed out waiting for pod to appear."
        kill "$JAC_PID" 2>/dev/null || true
        exit 1
    fi
done

echo "Pod found. Waiting for it to become Ready..."
if kubectl wait pod -n "$NAMESPACE" -l "$APP_LABEL" \
    --for=condition=Ready --timeout="${TIMEOUT}s" 2>/dev/null; then
    echo ""
    echo "Deployment ready!"
    kubectl get pods -n "$NAMESPACE"
    echo ""
    echo "Setting up port-forward on localhost:8080..."
    kubectl port-forward svc/jaseci-service 8080:8000 -n "$NAMESPACE" &
    PF_PID=$!
    sleep 2
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/docs | grep -q "200"; then
        echo "App is live at http://localhost:8080"
        echo ""
        echo "SSH tunnel from your local machine:"
        echo "  ssh -N -L 8080:localhost:8080 $(whoami)@$(hostname)"
    else
        echo "Port-forward up but app not responding yet — may still be initializing."
    fi
else
    echo "Pod did not become Ready within ${TIMEOUT}s. Check logs:"
    echo "  kubectl logs -n $NAMESPACE -l $APP_LABEL"
    kill "$JAC_PID" 2>/dev/null || true
    exit 1
fi
