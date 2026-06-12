#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="default"

echo "Tearing down jaseci deployment..."

kubectl delete all --all -n "$NAMESPACE" 2>/dev/null && echo "  Deleted pods, services, deployments" || echo "  Nothing to delete (all)"
kubectl delete pvc --all -n "$NAMESPACE" 2>/dev/null && echo "  Deleted PVCs" || echo "  No PVCs"
kubectl delete configmap --all -n "$NAMESPACE" 2>/dev/null && echo "  Deleted ConfigMaps" || echo "  No ConfigMaps"
kubectl delete secret --all -n "$NAMESPACE" 2>/dev/null && echo "  Deleted secrets" || echo "  No secrets"

# Kill any active port-forwards
if lsof -ti:8080 &>/dev/null; then
    kill $(lsof -ti:8080) 2>/dev/null && echo "  Killed port-forward on :8080"
fi

echo ""
echo "Teardown complete."
