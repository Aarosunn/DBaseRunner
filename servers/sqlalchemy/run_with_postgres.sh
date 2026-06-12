#!/usr/bin/env bash
# Spin up a throwaway Postgres in Docker, point the Flask/SQLAlchemy backend
# at it, and run the dev server on :8000 so the existing bench scripts in
# littleX-benchmarks/ work unchanged against a real Postgres.
#
# Usage:
#   ./run_with_postgres.sh                # foreground; Ctrl+C tears down
#   PORT=8080 ./run_with_postgres.sh      # override Flask port
#   PG_PORT=55432 ./run_with_postgres.sh  # override host-side Postgres port
#   KEEP_DB=1 ./run_with_postgres.sh      # don't drop container on exit
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-littlex-pg}"
PG_PORT="${PG_PORT:-55432}"
PG_USER="postgres"
PG_PASSWORD="postgres"
PG_DB="littlex"
PORT="${PORT:-8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v docker >/dev/null || { echo "docker not found in PATH"; exit 1; }

cleanup() {
    if [ "${KEEP_DB:-0}" = "1" ]; then
        echo
        echo "KEEP_DB=1: leaving container '$CONTAINER_NAME' running on :$PG_PORT"
        return
    fi
    echo
    echo "Stopping Postgres container '$CONTAINER_NAME'..."
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# If a stale container with the same name exists, remove it.
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "Removing existing container '$CONTAINER_NAME'..."
    docker rm -f "$CONTAINER_NAME" >/dev/null
fi

echo "Starting Postgres ($CONTAINER_NAME) on host port $PG_PORT..."
docker run -d \
    --name "$CONTAINER_NAME" \
    -e POSTGRES_USER="$PG_USER" \
    -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -e POSTGRES_DB="$PG_DB" \
    -p "$PG_PORT:5432" \
    postgres:16-alpine >/dev/null

echo -n "Waiting for Postgres to accept connections"
for i in $(seq 1 60); do
    if docker exec "$CONTAINER_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
        echo " — ready."
        break
    fi
    echo -n "."
    sleep 1
    if [ "$i" -eq 60 ]; then
        echo
        echo "Postgres did not become ready in 60s. Container logs:"
        docker logs "$CONTAINER_NAME" || true
        exit 1
    fi
done

export DATABASE_URL="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
echo "DATABASE_URL=$DATABASE_URL"
echo "Launching Flask on http://localhost:${PORT} ..."
echo

cd "$HERE"

# Prefer a local .venv if present, otherwise fall back to whatever python is on PATH.
if [ -x "$HERE/.venv/bin/python" ]; then
    PY="$HERE/.venv/bin/python"
elif command -v python3 >/dev/null; then
    PY="$(command -v python3)"
else
    PY="$(command -v python)"
fi

if ! "$PY" -c "import flask, sqlalchemy, psycopg" 2>/dev/null; then
    echo
    echo "Error: flask/sqlalchemy/psycopg not importable from: $PY"
    echo "Install deps first, e.g.:"
    echo "  python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
    echo "or:"
    echo "  uv sync"
    exit 1
fi

"$PY" -m flask --app src run --host 0.0.0.0 --port "$PORT" &
FLASK_PID=$!

# Forward signals to flask, then let the EXIT trap clean up Postgres.
shutdown() {
    kill -TERM "$FLASK_PID" 2>/dev/null || true
    wait "$FLASK_PID" 2>/dev/null || true
}
trap shutdown INT TERM

wait "$FLASK_PID"
