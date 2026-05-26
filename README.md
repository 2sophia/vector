# Sophia Vector

<p align="center">
  <img src="https://raw.githubusercontent.com/2sophia/vector/main/docs/hero.png" alt="Sophia Vector — vector + graph, un retrieval solo" width="100%">
</p>

OpenAI SDK Compatible Vector Store with Qdrant backend, a FalkorDB **knowledge graph**,
and a management frontend.

## Overview

Sophia Vector is a FastAPI vector storage/retrieval system with an OpenAI-compatible API
(`/v1/*`) and a **Next.js management frontend** (vector stores, directories, ingestion sources,
search). On top of classic hybrid search it builds a **knowledge graph** (FalkorDB) over the same
chunks and offers **graph-augmented retrieval**. Backend and frontend ship in the same Docker image.

## Features

- **OpenAI-compatible API** — drop-in vector store endpoints under `/v1/*`
- **Hybrid search** — dense + sparse retrieval with **cross-encoder reranking** (BGE-M3)
- **Multi-channel retrieval + RRF** — the vector channel is fused with an optional graph channel
  and a **lexical/exact-match channel** (full-text over the chunk index) via Reciprocal Rank
  Fusion, then a single rerank. The lexical channel catches codes/references the dense retriever
  misses (e.g. `D.lgs 231/2001`)
- **Wide format coverage** — documents, legacy Office, email, images (**OCR**) and **audio/video**
  (local Whisper transcription); a pre-parser layer converts whatever Docling can't read natively.
  See [Supported formats](#supported-formats)
- **Knowledge graph (FalkorDB)** — every document becomes `Document → Section → Chunk` with a
  reading-order `:NEXT` chain, plus deterministic **entity extraction** (GLiNER zero-shot NER +
  regex for IBAN / codice fiscale / P.IVA — **no LLM in ingestion**) and optional **typed
  relations** between entities (GLiNER-relex, zero-shot, off by default)
- **Configurable extraction schema** — the GLiNER engine is zero-shot, so *what* to extract is
  data: entity labels, relation labels and the relations toggle resolve through a
  **file → directory → sync → store → global** cascade, editable from the UI at every level
- **Graph-augmented retrieval** — Qdrant finds the chunks, the graph expands the neighbourhood
  (chunks sharing entities + adjacent chunks), then a unified rerank. Entity expansion is
  IDF-weighted so common "stopword-entities" don't dominate
- **Content curation** — near-duplicate chunks shared across many documents (headers/footers,
  boilerplate) are detected by content hash and suppressed at search time
- **Per-model device** — place GLiNER/relex and Whisper independently on CPU or a specific GPU
  (`SOPHIA_VECTOR_GLINER_DEVICE` / `ASR_DEVICE`), with graceful CPU fallback
- **Multi-source ingestion** — provider abstraction (SharePoint enabled; Google Drive/Workspace/S3
  as placeholders) with per-source **encrypted credentials** (Fernet)
- **Scheduled sync** — internal cron (configurable from the UI), replaces system crontab; run
  history with retention
- **Management frontend** — Next.js + NextAuth (email/password + optional Azure AD)
- **Async, idempotent ingestion** — background worker, content-hash dedup, safe re-ingest
  (index-new-then-delete-old)

## Supported formats

Anything Docling doesn't read natively is normalized by a **pre-parser layer**
(`utils/convert.py`) before chunking, so the same hybrid-search + graph pipeline works
across all of these:

| Category | Formats | Handling |
|---|---|---|
| Documents | PDF, DOCX, PPTX, XLSX, HTML/XHTML, Markdown, CSV, AsciiDoc, LaTeX | native (Docling) |
| Legacy / OpenDocument | DOC, PPT, XLS, RTF, ODT, ODP, ODS | → OOXML via LibreOffice |
| Email | EML, MSG | → HTML |
| Images | PNG, JPG, TIFF, BMP, GIF, WEBP | native, **OCR** for scanned text |
| Audio | WAV, MP3, M4A, OGG, FLAC, AAC, … | → VTT via local Whisper |
| Video | MP4, MOV, AVI, MKV, WEBM, … | audio track → VTT via Whisper |
| Subtitles | VTT | native |

OCR and audio/video transcription run **in-process**, no external service, and load lazily on
first use (like GLiNER). They default to **CPU** (no GPU required); the in-process models
(GLiNER/relex, Whisper) can be moved onto a GPU — see [GPU acceleration](#gpu-acceleration-optional).
Transcription is gated by `SOPHIA_VECTOR_ASR_ENABLED` with duration caps (60 min audio / 30 min
video, tunable). The accepted extensions are exposed at `GET /v1/files/supported-formats`
(single source of truth, consumed by upload, SharePoint and the UI).

The list is **verified end-to-end** by `tests/verify_formats.py`, which regenerates
[`tests/SUPPORTED_FORMATS.md`](tests/SUPPORTED_FORMATS.md).

## Architecture

```
                              ┌──────────────┐
                  ┌──────────>│   Qdrant     │  hybrid dense+sparse (vectors)
                  │           └──────────────┘
┌─────────────────┐           ┌──────────────┐
│  Sophia Vector  │──────────>│  FalkorDB    │  knowledge graph (doc→section→chunk→entity)
│   (FastAPI)     │           └──────────────┘
│  + workers:     │           ┌──────────────┐
│   vector        │──────────>│  MongoDB     │  jobs, metadata, sources, schedules, users
│   sharepoint    │           └──────────────┘
│   scheduler     │           ┌──────────────┐
└─────────────────┘──────────>│  Docling     │  document parsing + hybrid chunking (IBM)
        │                     └──────────────┘
        │                     ┌──────────────┐
        ├────────────────────>│  BGE-M3      │  embeddings + cross-encoder rerank
        │                     └──────────────┘
        │                     ┌──────────────┐
        └────────────────────>│  GLiNER      │  zero-shot NER + relations (CPU or GPU)
                              └──────────────┘
```

The graph is an **additive** layer: if FalkorDB is down, ingestion and search on Qdrant keep
working (best-effort writes). Set `SOPHIA_VECTOR_GRAPH_ENABLED=false` to disable it entirely.

## Prerequisites

- Docker & Docker Compose
- **Qdrant** vector DB — https://hub.docker.com/r/qdrant/qdrant
- **MongoDB** — https://hub.docker.com/_/mongo
- **FalkorDB** knowledge graph — https://github.com/FalkorDB/FalkorDB
- **Docling** parser — https://github.com/docling-project/docling-serve
- **BGE-M3** embeddings + rerank — https://hub.docker.com/r/sophiacloud/bge-m3-service

## Quick Start

### Development (no container)

```bash
cp -n .env.example .env          # set at least NEXTAUTH_SECRET
./dev-start.sh                   # backend :8100 + frontend :3100
# UI at http://localhost:3100 — the first login becomes admin
```

> **venv gotcha**: the `.venv` wrappers (`.venv/bin/uvicorn`, `pip`) have a stale absolute-path
> shebang. Always use **`.venv/bin/python -m <module>`** (already done in `dev-start.sh`).

### Production (Docker)

The `sophia-vector` service in `docker-compose.yml` runs the published image. Build & push with:

```bash
./compile-and-publish.sh [version]            # CPU image, multi-arch (amd64 + arm64) + push
./compile-and-publish.sh [version] cu130      # GPU image (torch cu130), amd64 → :<version>-cu130
# or directly:
docker buildx build --platform linux/amd64 -t sophiacloud/vector:0.3.0-alpha --push .
```

On the prod host, copy `.env.prod` → `.env` (only secrets + `NEXTAUTH_URL`; service URLs are
baked into the image), then:

```bash
docker compose pull sophia-vector && docker compose up -d sophia-vector
```

### GPU acceleration (optional)

The default image is **CPU-only** (torch CPU wheels): GLiNER, GLiNER-relex and Whisper run on CPU,
which is fine for moderate volumes. To run them on a GPU, build the **GPU flavor**
(`Dockerfile.cu130`, torch cu130 / CUDA 13.0, ~2 GB larger):

```bash
./compile-and-publish.sh 0.3.0-alpha cu130    # → sophiacloud/vector:0.3.0-alpha-cu130
```

Then point the compose service at the `-cu130` image, give it the GPU (host needs the NVIDIA driver
+ `nvidia-container-toolkit`), and opt the models in via env:

```yaml
# docker-compose.yml (sophia-vector service)
image: sophiacloud/vector:0.3.0-alpha-cu130
deploy:
  resources:
    reservations:
      devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }]
environment:
  SOPHIA_VECTOR_GLINER_DEVICE: cuda    # GLiNER + GLiNER-relex on GPU
  SOPHIA_VECTOR_ASR_DEVICE: cuda       # Whisper on GPU
```

On the CPU image these knobs are inert (no CUDA → graceful fallback to CPU), so the same env is
safe everywhere.

## Configuration

All application env vars are prefixed **`SOPHIA_VECTOR_`** (the in-image defaults already point to
the compose service names). NextAuth frontend vars are unprefixed (`NEXTAUTH_SECRET`,
`NEXTAUTH_URL`, `MONGODB_URI`, `AUTH_DB`, `AZURE_AD_*`). See `.env.example` for the full list.

| Variable                              | Default                                | Description                              |
|---------------------------------------|----------------------------------------|------------------------------------------|
| `SOPHIA_VECTOR_QDRANT_URL`            | `http://localhost:6333`                | Qdrant URL                               |
| `SOPHIA_VECTOR_MONGODB_URI`           | `mongodb://localhost:27017/sophia_vector` | Mongo connection (DB in the path)     |
| `SOPHIA_VECTOR_DOCLING_URL`           | `http://localhost:5001`                | Docling parser URL                       |
| `SOPHIA_VECTOR_EMBEDDINGS_URL`        | `http://localhost:8004`                | BGE-M3 embeddings + rerank URL           |
| `SOPHIA_VECTOR_SECRET_KEY`            | —                                      | Fernet key, encrypts source secrets      |
| `SOPHIA_VECTOR_PARSER_MODEL_MAX_TOKENS` | `512`                                | Chunk size in tokens (Docling)           |
| `SOPHIA_VECTOR_PARSER_MAX_WAIT_SECONDS` | `36000`                              | Per-doc parse timeout (≤ docling's max)  |
| `SOPHIA_VECTOR_PARSER_USE_OCR`        | `true`                                 | OCR scanned PDFs/images (force_ocr off)  |
| `SOPHIA_VECTOR_ASR_ENABLED`           | `true`                                 | Transcribe audio/video locally (Whisper) |
| `SOPHIA_VECTOR_ASR_MODEL`             | `small`                                | Whisper model (tiny…large-v3)            |
| `SOPHIA_VECTOR_ASR_MAX_AUDIO_MINUTES` | `60`                                   | Reject audio longer than this            |
| `SOPHIA_VECTOR_ASR_MAX_VIDEO_MINUTES` | `30`                                   | Reject video longer than this            |
| `SOPHIA_VECTOR_GRAPH_ENABLED`         | `true`                                 | Enable the FalkorDB knowledge graph      |
| `SOPHIA_VECTOR_FALKOR_HOST`           | `localhost`                            | FalkorDB host                            |
| `SOPHIA_VECTOR_FALKOR_PASSWORD`       | `falkordb`                             | FalkorDB password (`requirepass`)        |
| `SOPHIA_VECTOR_FALKOR_GRAPH_PREFIX`   | _(empty)_                              | Graph-name namespace (multi-project)     |
| `SOPHIA_VECTOR_GLINER_MODEL`          | `urchade/gliner_multi-v2.1`            | GLiNER NER model (multilingual)          |
| `SOPHIA_VECTOR_GLINER_LABELS`         | `organizzazione,persona,…`             | Entity labels (CSV, zero-shot default)   |
| `SOPHIA_VECTOR_GLINER_DEVICE`         | `cpu`                                  | GLiNER/relex device (`cpu`/`cuda`/`auto`)|
| `SOPHIA_VECTOR_ASR_DEVICE`            | `cpu`                                  | Whisper device (`cpu`/`cuda`/`auto`)     |
| `SOPHIA_VECTOR_RELATIONS_ENABLED`     | `false`                                | Typed relation extraction (GLiNER-relex) |
| `SOPHIA_VECTOR_RELATIONS_LABELS`      | `pubblicato da,emesso da,…`            | Relation labels (CSV, zero-shot default) |
| `SOPHIA_VECTOR_CURATION_ENABLED`      | `true`                                 | Boilerplate detection + suppression      |
| `SOPHIA_VECTOR_INTERNAL_API_URL`      | `http://127.0.0.1:8100`                | Scheduler → backend (internal)           |

## API Endpoints

```bash
# Vector stores
POST|GET /v1/vector_stores              GET|DELETE /v1/vector_stores/{id}
GET|DELETE /v1/vector_stores/{id}/files            # list / attach / remove files
POST /v1/vector_stores/{id}/search                 # multi-channel + graph-augmented search
GET|PUT /v1/vector_stores/{id}/schema              # extraction schema (store scope)
GET /v1/vector_stores/{id}/curation                # content-curation stats

# Files
POST|GET /v1/files    GET|DELETE /v1/files/{id}    GET /v1/files/{id}/content
GET /v1/files/supported-formats                    # accepted extensions (source of truth)

# Directories (slug + custom properties; how the UI groups files in a store)
POST|GET /v1/directories    GET|PATCH|DELETE /v1/directories/{id}
GET|PUT|DELETE /v1/directories/{id}/schema         # extraction schema (directory scope)

# Ingestion sources (multi-provider, encrypted secrets)
GET /v1/sources/types    POST|GET /v1/sources    GET /v1/sources/{id}/browse
POST /v1/ingest/sharepoint    POST /v1/ingest/sharepoint/{id}/sync    DELETE /v1/ingest/sharepoint/{id}
GET|PUT|DELETE /v1/ingest/sharepoint/{id}/schema   # extraction schema (sync scope)

# Scheduled sync (internal cron)
GET|PUT /v1/sync/schedule/{type}    GET /v1/sync/runs/{type}
```

### Search example (with graph expansion)

```bash
curl -X POST http://localhost:8100/v1/vector_stores/vs_abc123/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "obblighi di trasparenza verso il cliente",
    "max_num_results": 10,
    "graph_expand": true,
    "graph_neighbors": 20,
    "graph_df_max": 0.5
  }'
```

The vector and (when `graph_expand` is set) graph channels are joined with a **lexical channel**
that fires automatically when the query contains codes/references, then fused with RRF and a single
rerank. Each result carries `attributes._source` (`qdrant` / `lexical` / `graph:mentions` /
`graph:next`) and `attributes._via` (the bridge entities) so you can see *why* a chunk was retrieved.

## Knowledge graph

One FalkorDB graph per vector store (same name as the Qdrant collection). Nodes:
`:Document` → `:Section` → `:Chunk` (with `qdrant_point_id` bridging to Qdrant), `:Chunk -[:NEXT]->`
for reading order, and `:Chunk -[:MENTIONS]-> :Entity` (entities shared across documents via
`MERGE` → cross-document resolution). With relation extraction enabled, entities are also linked by
**typed** `:REL {type,score}` edges (GLiNER-relex, e.g. `DECRETO —emesso da→ PRESIDENTE DELLA REPUBBLICA`).
Inspect it from the FalkorDB UI or via Cypher, e.g.:

```cypher
MATCH (d:Document)-[:HAS_CHUNK|HAS_SECTION*]->(:Chunk)-[:MENTIONS]->(e:Entity)
WITH e, collect(DISTINCT d.filename) AS docs WHERE size(docs) > 1
RETURN e.type, e.name, docs ORDER BY size(docs) DESC LIMIT 20
```

## Benchmark

`scripts/bench_search.py` runs a golden set with graph expansion **on vs off** and reports the
gold-chunk rank, Recall@3 and MRR — to measure whether the graph actually helps:

```bash
.venv/bin/python scripts/bench_search.py <vector_store_id>
```

## API Documentation

- API reference (Scalar): `http://localhost:8100/docs`
- Raw OpenAPI schema: `http://localhost:8100/openapi.json`

## Version

Current version: **0.3.0-alpha**

## License

Service code, API, and orchestration logic are licensed under the
**PolyForm Noncommercial License 1.0.0**. © Sophia AI Cloud — https://www.sophia-cloud.com
