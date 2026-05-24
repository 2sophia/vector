#!/usr/bin/env bash
# Dev launcher: backend (uvicorn) + frontend (next dev).
# Ctrl+C ferma tutto in modo pulito.
set -euo pipefail

cd "$(dirname "$0")"

# Carica il .env nell'ambiente del processo. Le SOPHIA_VECTOR_* le legge già
# pydantic dal file, ma le env "legacy" lette via os.getenv (PARSER_*, INGEST_*,
# AZURE_*) NO: senza questo restano ai default hardcoded. Con `set -a` vengono
# esportate e diventano visibili a os.getenv di backend e worker.
if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "❌ .venv/bin/python non trovato. Crea il venv e 'pip install -r requirements.txt' prima." >&2
  exit 1
fi
if [[ ! -d frontend/node_modules ]]; then
  echo "⚠️  frontend/node_modules mancante — eseguo npm install…"
  (cd frontend && npm install)
fi

BACKEND_PORT="${SOPHIA_VECTOR_BACKEND_PORT:-8100}"
FRONTEND_PORT="${SOPHIA_VECTOR_FRONTEND_PORT:-3100}"

LOG_DIR=".data/dev-logs"
mkdir -p "$LOG_DIR"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

pids=()
cleanup() {
  echo
  echo "🛑  fermo backend e frontend…"
  for pid in "${pids[@]:-}"; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  echo "✅  done."
}
trap cleanup INT TERM EXIT

echo "🐍  backend  → http://localhost:${BACKEND_PORT}   (log: $BACKEND_LOG)"
# python -m uvicorn (non .venv/bin/uvicorn): lo shebang dei wrapper si rompe se
# il venv viene spostato; il python del venv invece resta valido.
.venv/bin/python -m uvicorn main:app --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
pids+=("$!")

echo "⚛️   frontend → http://localhost:${FRONTEND_PORT}   (log: $FRONTEND_LOG)"
(cd frontend && PORT="$FRONTEND_PORT" FASTAPI_URL="http://localhost:${BACKEND_PORT}" npm run dev) >"$FRONTEND_LOG" 2>&1 &
pids+=("$!")

echo
echo "📜  tail dei log (Ctrl+C per fermare tutto):"
tail -n 0 -F "$BACKEND_LOG" "$FRONTEND_LOG"
