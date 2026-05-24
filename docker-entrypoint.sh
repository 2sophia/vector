#!/usr/bin/env bash
set -euo pipefail

# ─── Privilege drop ──────────────────────────────────────────────────────────
# Parte da root solo per sistemare i permessi del volume /app/storage
# (un volume montato può avere file root-owned da deploy precedenti), poi
# si re-esegue come `sophia` via gosu.
if [ "$(id -u)" = "0" ]; then
    echo "[entrypoint] Running as root — fixing /app/storage ownership and dropping to sophia"
    chown -R sophia:sophia /app/storage 2>/dev/null || true
    exec gosu sophia "$0" "$@"
fi

BACKEND_PID=
FRONTEND_PID=

cleanup() {
    echo "[entrypoint] Shutting down..."
    [ -n "$BACKEND_PID" ]  && kill "$BACKEND_PID"  2>/dev/null || true
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    wait
    exit 0
}

trap cleanup SIGTERM SIGINT

BACKEND_PORT="${SOPHIA_VECTOR_BACKEND_PORT:-8100}"
FRONTEND_PORT="${SOPHIA_VECTOR_FRONTEND_PORT:-3100}"

echo "[entrypoint] Starting backend on :${BACKEND_PORT}"
# I worker (vector, sharepoint) sono subprocess gestiti dal lifespan FastAPI,
# quindi un solo processo uvicorn (--workers 1).
uvicorn main:app --host 0.0.0.0 --port "$BACKEND_PORT" --workers 1 &
BACKEND_PID=$!

if [[ "${1:-}" == "--frontend" ]]; then
    export FASTAPI_URL="${FASTAPI_URL:-http://localhost:${BACKEND_PORT}}"
    echo "[entrypoint] Starting frontend on :${FRONTEND_PORT} -> backend at ${FASTAPI_URL}"
    cd /app/frontend
    HOSTNAME=0.0.0.0 PORT="$FRONTEND_PORT" node server.js &
    FRONTEND_PID=$!
fi

wait -n
echo "[entrypoint] A process exited unexpectedly, shutting down..."
cleanup
