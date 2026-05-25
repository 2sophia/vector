#!/usr/bin/env bash
# Build dell'immagine Sophia Vector (backend FastAPI + frontend Next.js nello
# stesso container) e push sul registry.
#
#   ./compile-and-publish.sh [versione] [cpu|cuda]
#
#   cpu  (default) → Dockerfile,      torch CPU-only, multi-arch (amd64+arm64),
#                    tag sophiacloud/vector:<versione>
#   cuda           → Dockerfile.cuda, torch cu130, solo amd64 (wheel x86_64),
#                    tag sophiacloud/vector:<versione>-cuda
#                    (richiede host con GPU + nvidia-container-toolkit a runtime)
set -euo pipefail

VERSION="${1:-0.3.0-alpha}"
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
  cuda)
    docker buildx build \
      -f Dockerfile.cuda \
      --platform linux/amd64 \
      -t "sophiacloud/vector:${VERSION}-cuda" \
      --push \
      .
    ;;
  *)
    echo "flavor sconosciuto: '$FLAVOR' (usa 'cpu' o 'cuda')" >&2
    exit 1
    ;;
esac
