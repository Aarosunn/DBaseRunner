#!/usr/bin/env bash
# Build an image-based backend's app image straight into minikube, with the tag
# the manifest expects. ONLY postgres and neo4j ship as images; sqlalchemy
# (ConfigMap) and jac (--scale) build nothing and are rejected here.
#
# Why this exists: minikube uses imagePullPolicy: Never, so the pod serves only
# a locally-present tag. Re-using a tag does NOT replace the running layer, so
# the manifests pin immutable tags (postgres-app:v1, neo4j-app:v1). This script
# builds that exact tag into minikube's docker (eval docker-env), skipping the
# flaky `minikube image load`. It does NOT deploy or validate — that's the curl
# check + the harness run after deploy.
#
# Usage:  ./build.sh postgres            # builds the tag from k8s/postgres/*.yaml
#         ./build.sh neo4j v2            # override tag (also bumps the manifest)
set -euo pipefail
cd "$(dirname "$0")"

BACKEND="${1:?usage: ./build.sh <postgres|neo4j> [tag]}"
case "$BACKEND" in
  postgres|neo4j) ;;
  sqlalchemy) echo "sqlalchemy ships via ConfigMap (no image); use k8s-configmap.sh." >&2; exit 2 ;;
  jac)        echo "jac deploys via servers/jac/deploy.sh (--scale, no image)."        >&2; exit 2 ;;
  *)          echo "unknown backend: $BACKEND (image backends: postgres, neo4j)"        >&2; exit 2 ;;
esac

IMAGE="dbaserunner/${BACKEND}-app"
# Tag: use the override, else read what the manifest currently pins.
if [[ -n "${2:-}" ]]; then
  TAG="$2"
else
  TAG="$(grep -oE "${IMAGE}:[^\"[:space:]]+" k8s/${BACKEND}/*.yaml | head -1 | cut -d: -f2)"
  [[ -n "$TAG" ]] || { echo "could not read ${IMAGE} tag from k8s/${BACKEND}/*.yaml" >&2; exit 1; }
fi

echo "==> building ${IMAGE}:${TAG} into minikube from servers/${BACKEND}"
eval "$(minikube docker-env)"
docker build -t "${IMAGE}:${TAG}" "servers/${BACKEND}"
eval "$(minikube docker-env -u)"

# If an override tag was given, point the manifest at it.
if [[ -n "${2:-}" ]]; then
  sed -i "s|${IMAGE}:[^\"[:space:]]*|${IMAGE}:${TAG}|" k8s/${BACKEND}/*.yaml
  echo "  manifest k8s/${BACKEND}/ now pins ${IMAGE}:${TAG}"
fi

echo "==> built ${IMAGE}:${TAG}. Deploy via orchestrate.sh, then curl-check (401 = good)."
