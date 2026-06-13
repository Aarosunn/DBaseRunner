#!/usr/bin/env bash
# Session-start hygiene for a cluster run. Backend-agnostic — run it FIRST every
# session, regardless of which backends you touch. Pure cleanup + status; it
# changes no cluster state beyond killing stray local processes.
#
# Solves the recurring failure modes:
#   - stray `jac start` dev servers squatting LOCAL_PORT -> orchestrate health 404
#   - leftover `kubectl port-forward` tunnels holding a port
#   - running on a busy port (the 8000 squatter)
#
# Usage:  ./preflight.sh            # checks ports 8001 + 8080 (the run defaults)
#         PORTS="8001 8090" ./preflight.sh
set -euo pipefail

PORTS="${PORTS:-8001 8080}"

echo "==> preflight: clearing stray processes"
pkill -9 -f "jac start"        2>/dev/null && echo "  killed stray 'jac start' servers" || true
pkill -f "kubectl port-forward" 2>/dev/null && echo "  killed stray port-forwards"      || true
sleep 1

echo "==> port check (want each FREE):"
busy=0
for p in $PORTS; do
  if ss -tlnp 2>/dev/null | grep -q ":${p} "; then
    echo "  PORT ${p}: BUSY -> $(ss -tlnp 2>/dev/null | grep ":${p} " | head -1)"
    busy=1
  else
    echo "  PORT ${p}: free"
  fi
done

echo "==> cluster pods:"
kubectl get pods 2>&1 | sed 's/^/  /'

if [[ "$busy" -ne 0 ]]; then
  echo ""
  echo "WARNING: a target port is busy. Free it or pass a different LOCAL_PORT before running." >&2
fi
echo "==> preflight done."
