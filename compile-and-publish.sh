#!/usr/bin/env bash
# Build dell'immagine Sophia Vector (backend FastAPI + frontend Next.js nello
# stesso container) e push sul registry.
#
#   ./compile-and-publish.sh [versione] [all|cpu|cu130]
#
#   Argomenti POSIZIONE-INDIPENDENTI: cpu/cu130/all/both sono riconosciuti come flavor,
#   qualunque altro argomento è la versione. Esempi:
#     ./compile-and-publish.sh               → versione di config, ENTRAMBE le varianti
#     ./compile-and-publish.sh cu130         → versione di config, solo cu130
#     ./compile-and-publish.sh 0.6.1-alpha cpu
#
#   all   (default) → costruisce e pusha ENTRAMBE le varianti, una dopo l'altra
#   cpu             → Dockerfile,       torch CPU-only, multi-arch (amd64+arm64),
#                     tag sophiacloud/vector:<versione>
#   cu130           → Dockerfile.cu130, torch cu130 (CUDA 13.0), solo amd64,
#                     tag sophiacloud/vector:<versione>-cu130
#                     (richiede host con GPU + nvidia-container-toolkit a runtime)
set -euo pipefail
cd "$(dirname "$0")"

# Versione di default = APP_VERSION da utils/config.py (fonte di verità). Gli argomenti sono
# classificati per valore, NON per posizione: un flavor noto è il flavor, il resto è la versione
# → così `compile-and-publish.sh cu130` NON scambia "cu130" per una versione (tag :cu130 errato).
CONFIG_VERSION="$(grep -oP 'APP_VERSION:\s*str\s*=\s*"\K[^"]+' utils/config.py || true)"
VERSION=""
FLAVOR=""
for arg in "$@"; do
  case "$arg" in
    cpu|cu130|all|both) FLAVOR="$arg" ;;
    *)                  VERSION="$arg" ;;
  esac
done
VERSION="${VERSION:-${CONFIG_VERSION:?impossibile leggere APP_VERSION da utils/config.py — passala come argomento}}"
FLAVOR="${FLAVOR:-all}"

build_cpu() {
  echo "🏗️  Building sophiacloud/vector:${VERSION} (cpu, multi-arch amd64+arm64)"
  docker buildx build \
    -f Dockerfile \
    --platform linux/amd64,linux/arm64 \
    -t "sophiacloud/vector:${VERSION}" \
    --push \
    .
}

build_cu130() {
  echo "🏗️  Building sophiacloud/vector:${VERSION}-cu130 (cuda 13.0, amd64)"
  docker buildx build \
    -f Dockerfile.cu130 \
    --platform linux/amd64 \
    -t "sophiacloud/vector:${VERSION}-cu130" \
    --push \
    .
}

case "$FLAVOR" in
  cpu)      build_cpu;   PUBLISHED="sophiacloud/vector:${VERSION}" ;;
  cu130)    build_cu130; PUBLISHED="sophiacloud/vector:${VERSION}-cu130" ;;
  all|both) build_cpu; build_cu130  # entrambe, sequenziali (fail-fast se la prima fallisce)
            PUBLISHED="sophiacloud/vector:${VERSION} + :${VERSION}-cu130" ;;
  *)
    echo "flavor sconosciuto: '$FLAVOR' (usa 'all', 'cpu' o 'cu130')" >&2
    exit 1
    ;;
esac

echo "✅ Pubblicato ${PUBLISHED}"
