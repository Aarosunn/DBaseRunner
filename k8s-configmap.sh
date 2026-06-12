#!/usr/bin/env bash
# Populate a backend's source ConfigMap from the CURRENT repo files, so the
# no-build backends (jac, sqlalchemy) run the latest server code on the cluster.
#
# Image-based backends (postgres, neo4j) ship code in their Docker images and do
# not use a ConfigMap — this script is a no-op for them.
#
# Per-backend by design: populating one backend's ConfigMap never touches another.
# Called standalone or from orchestrate.sh before `kubectl apply -f k8s/<b>/`.
#
# Usage:  ./k8s-configmap.sh <jac|sqlalchemy|postgres|neo4j>
# Env:    NAMESPACE (default: default)
set -euo pipefail
cd "$(dirname "$0")"

NAMESPACE="${NAMESPACE:-default}"
BACKEND="${1:?usage: ./k8s-configmap.sh <jac|sqlalchemy|postgres|neo4j>}"

apply_cm() {  # name, then --from-file args
  local name="$1"; shift
  kubectl create configmap "$name" -n "$NAMESPACE" "$@" \
    --dry-run=client -o yaml | kubectl apply -n "$NAMESPACE" -f -
  echo "  configmap/${name} populated from current source"
}

case "$BACKEND" in
  jac)
    # Whole-file keys; mounted at /src, the initContainer copies them into /app.
    apply_cm dbaserunner-jac-src \
      --from-file=servers/jac/main.jac \
      --from-file=servers/jac/server.jac \
      --from-file=servers/jac/jac.toml
    ;;
  sqlalchemy)
    # Flattened keys remapped to real paths by the manifest's `items:` block.
    apply_cm dbaserunner-sqlalchemy-src \
      --from-file=requirements.txt=servers/sqlalchemy/requirements.txt \
      --from-file=src__init__.py=servers/sqlalchemy/src/__init__.py \
      --from-file=src_models.py=servers/sqlalchemy/src/models.py \
      --from-file=src_routes_user.py=servers/sqlalchemy/src/routes/user.py \
      --from-file=src_routes_walker.py=servers/sqlalchemy/src/routes/walker.py
    ;;
  postgres|neo4j)
    echo "  ${BACKEND}: image-based (dbaserunner/${BACKEND}-app); no ConfigMap. "\
"Rebuild+push the image instead."
    ;;
  *)
    echo "unknown backend: $BACKEND" >&2; exit 2 ;;
esac
