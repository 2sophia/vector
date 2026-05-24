# ==============================================================================
# Stage 1: build frontend (Next.js standalone)
# ==============================================================================
FROM node:22-slim AS frontend-builder

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# Next.js richiede un .env a build time — rimuovi il symlink rotto e crea un file vuoto
RUN rm -f .env && touch .env
RUN npm run build

# ==============================================================================
# Stage 2: backend Python (FastAPI) + frontend opzionale
# ==============================================================================
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Default di rete docker. I default del codice puntano a localhost (per il dev,
# che gira senza container); nell'immagine prod devono parlare ai servizi del
# compose. Sovrascrivibili da env/`.env` se la topologia cambia.
ENV SOPHIA_VECTOR_QDRANT_URL=http://qdrant:6333 \
    SOPHIA_VECTOR_DOCLING_URL=http://parser:5001 \
    SOPHIA_VECTOR_EMBEDDINGS_URL=http://embeddings:8004 \
    SOPHIA_VECTOR_FALKOR_HOST=falkordb \
    SOPHIA_VECTOR_MONGODB_URI=mongodb://mongodb:27017/sophia_vector \
    MONGODB_URI=mongodb://mongodb:27017 \
    SOPHIA_VECTOR_PARSER_MAX_WAIT_SECONDS=1700

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gosu \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Utente non privilegiato per il runtime
RUN useradd --create-home --uid 1000 --shell /bin/bash sophia

# Dipendenze Python — torch CPU-only PRIMA, sennò `requirements` tira la build
# CUDA da ~2GB (qui gira tutto su CPU: GLiNER è leggero).
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir --timeout 300 --retries 5 -r requirements.txt

# Backend (struttura alla root)
COPY main.py .
COPY routers/ ./routers/
COPY utils/ ./utils/
COPY workers/ ./workers/

# Frontend standalone build
COPY --from=frontend-builder /build/.next/standalone/ ./frontend/
COPY --from=frontend-builder /build/.next/static/ ./frontend/.next/static/
COPY --from=frontend-builder /build/public/ ./frontend/public/

# Storage persistente (file caricati per re-ingestion)
RUN mkdir -p /app/storage/files /app/storage/documents
VOLUME /app/storage

# Entrypoint: parte da root per sistemare i permessi del volume, poi droppa a sophia
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
RUN chown -R sophia:sophia /app

EXPOSE 8100 3100

ENTRYPOINT ["bash", "/app/docker-entrypoint.sh"]
CMD []
