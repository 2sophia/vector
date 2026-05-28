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

VERSION="${1:-0.4.2-alpha}"
FLAVOR="${2:-cpu}"

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
