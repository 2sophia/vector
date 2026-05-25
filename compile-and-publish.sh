#!/usr/bin/env bash
# Build multi-platform dell'immagine Sophia Vector (backend FastAPI + frontend
# Next.js nello stesso container) e push sul registry.
set -euo pipefail

VERSION="${1:-0.2.0-alpha}"

docker buildx build \
  -f Dockerfile \
  --platform linux/amd64,linux/arm64 \
  -t "sophiacloud/vector:${VERSION}" \
  --push \
  .
