#!/usr/bin/env bash
# Build dell'immagine Sophia Vector (backend FastAPI + frontend Next.js nello
# stesso container) e push sul registry.
#
#   ./compile-and-publish.sh [versione] [cpu|cu130]
#
#   cpu   (default) → Dockerfile,       torch CPU-only, multi-arch (amd64+arm64),
#                     tag sophiacloud/vector:<versione>
#   cu130            → Dockerfile.cu130, torch cu130 (CUDA 13.0), solo amd64,
#                     tag sophiacloud/vector:<versione>-cu130
#                     (richiede host con GPU + nvidia-container-toolkit a runtime)
set -euo pipefail
cd "$(dirname "$0")"

# Default = APP_VERSION dichiarata in utils/config.py (fonte di verità della versione).
# Override esplicito con il 1° argomento. Così un push senza argomenti non resta indietro.
CONFIG_VERSION="$(grep -oP 'APP_VERSION:\s*str\s*=\s*"\K[^"]+' utils/config.py || true)"
VERSION="${1:-${CONFIG_VERSION:?impossibile leggere APP_VERSION da utils/config.py — passala come 1° argomento}}"
FLAVOR="${2:-cpu}"

echo "🏗️  Building sophiacloud/vector:${VERSION} (${FLAVOR})"

case "$FLAVOR" in
  cpu)
    docker buildx build \
      -f Dockerfile \
      --platform linux/amd64,linux/arm64 \
      -t "sophiacloud/vector:${VERSION}" \
      --push \
      .
    ;;
  cu130)
    docker buildx build \
      -f Dockerfile.cu130 \
      --platform linux/amd64 \
      -t "sophiacloud/vector:${VERSION}-cu130" \
      --push \
      .
    ;;
  *)
    echo "flavor sconosciuto: '$FLAVOR' (usa 'cpu' o 'cu130')" >&2
    exit 1
    ;;
esac
