#!/usr/bin/env bash
# Orchestrate full benchmark run on clarity k8s cluster.
# Usage: ./orchestrate.sh [--backend jac|postgres|sqlalchemy|neo4j|all]
#
# Sequence per backend:
#   1. kubectl apply -f k8s/<backend>/
#   2. Wait for pod health check
#   3. python harness.py --backend <backend> --url <svc_url>
#   4. kubectl delete -f k8s/<backend>/
#
# TODO: implement when k8s YAMLs and harness.py are ready

set -euo pipefail

echo "orchestrate.sh: not yet implemented" >&2
exit 1
