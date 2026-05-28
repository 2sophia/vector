# Sophia Vector

#### OpenAI SDK Compatible Vector Store with Qdrant backend, a FalkorDB **knowledge graph**, and a management frontend.

<p align="center">
  <img src="https://raw.githubusercontent.com/2sophia/vector/main/docs/hero.png" alt="Sophia Vector — vector + graph, un retrieval solo" width="100%">
</p>

---

### Built in Admin System

<p align="center">
  <img src="https://raw.githubusercontent.com/2sophia/vector/main/docs/demo.gif" alt="Sophia Vector — demo (stores, ingestion, search, knowledge graph)" width="100%">
</p>

---

### OpenAI Compatible api & more

<p align="center">
  <img src="https://raw.githubusercontent.com/2sophia/vector/main/docs/api.png" alt="Sophia Vector — OpenAI-compatible API reference (Scalar)" width="100%">
</p>

---

### MCP server — RAG & NLP tools for your agents

<p align="center">
  <img src="https://raw.githubusercontent.com/2sophia/vector/main/docs/mcp.png" alt="Sophia Vector — MCP server (search + NLP tools over Model Context Protocol)" width="100%">
</p>

## Run it — Docker image

Published on Docker Hub: **[`sophiacloud/vector`](https://hub.docker.com/r/sophiacloud/vector)** (backend **and** frontend in one image, multi-arch amd64/arm64).

```bash
docker run -d --name sophia-vector \
  -p 8100:8100 -p 3100:3100 \
  -e SOPHIA_VECTOR_QDRANT_URL=http://qdrant:6333 \
  -e SOPHIA_VECTOR_MONGODB_URI=mongodb://mongo:27017 \
  -e SOPHIA_VECTOR_DOCLING_URL=http://parser:5001 \
  -e SOPHIA_VECTOR_EMBEDDINGS_URL=http://embeddings:8004 \
  -e NEXTAUTH_SECRET=change-me \
  -e SOPHIA_VECTOR_SECRET_KEY=change-me \
  -v sophia_vector_data:/app/storage \
  sophiacloud/vector:0.6.0-alpha --frontend
```

- **API** on `:8100` (OpenAI-compatible `/v1/*`), **management UI** on `:3100` (first login becomes admin)
- `--frontend` starts the UI too; drop it to run the backend only
- Needs **Qdrant**, **MongoDB**, **Docling** and a **BGE-M3** endpoint reachable — the whole stack is wired up in [`docker-compose.yml`](docker-compose.yml) (recommended: `docker compose up -d`)
- GPU build available as `sophiacloud/vector:0.6.0-alpha-cu130` (see [GPU acceleration](#gpu-acceleration-optional))

> Full env reference in [Configuration](#configuration). For a one-command local stack use Compose; the bare `docker run` above is the minimal shape.

## Overview

Sophia Vector is a FastAPI vector storage/retrieval system with an OpenAI-compatible API
(`/v1/*`) and a **Next.js management frontend** (vector stores, directories, ingestion sources,
search). On top of classic hybrid search it builds a **knowledge graph** (FalkorDB) over the same
chunks and offers **graph-augmented retrieval**. Backend and frontend ship in the same Docker image.

## Where Sophia Vector fits

Sophia Vector isn't a Milvus alternative (Milvus is a billion-scale, distributed **vector
database**) nor a RAGFlow replacement (a mature, full-featured **RAG engine**). It's an
**OpenAI-compatible, self-hosted RAG store**: drop-in `/v1/*` endpoints, document parsing
(Docling), hybrid search with reranking — and, built in rather than bolted on, a **knowledge
graph** and **semantic deduplication you can maintain without re-ingesting**. If you already use
the OpenAI SDK and want a self-hosted backend with a graph and a content-curation layer, that's
the gap it fills.

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
  reading-order `:NEXT` chain. The graph is **on out-of-the-box**, populated by **high-precision
  regex** (emails, URLs, IBAN/ISIN, amounts, dates, legal references, Italian codice fiscale /
  P.IVA — light, no model download). **Zero-shot NER** (GLiNER) for the "soft" entities (authorities,
  bodies, products) and **typed relations** (GLiNER-relex) are **opt-in** — heavier, noisier, off by
  default. **No LLM in ingestion**
- **Zero-shot classification (opt-in)** — GliClass tags chunks by **document type / theme /
  sensitivity** for faceting and filtering at search time; this is where *abstract* categories live
  (vs entity spans). Off by default (`pip install gliclass` to enable)
- **Configurable extraction schema** — the engine is zero-shot, so *what* to extract is data:
  entity labels, relation labels and the relations toggle resolve through a
  **file → directory → sync → store → global** cascade, editable from the UI at every level.
  Defaults lean **banking / legal** (financial labels, Italian regulatory references) — point them
  at your own domain by editing the labels and thresholds
- **Graph-augmented retrieval** — Qdrant finds the chunks, the graph expands the neighbourhood
  (chunks sharing entities + adjacent chunks), then a unified rerank. Entity expansion is
  IDF-weighted so common "stopword-entities" don't dominate
- **Content curation + semantic dedup** — exact boilerplate (same text across many documents:
  headers/footers, disclaimers) is detected by content hash and suppressed at search time;
  **near-duplicates** ("same thing, different words") are found by **dense ∩ sparse agreement**
  and *marked* (kept, not deleted), keeping one representative per cluster — the dense-vs-sparse
  *mismatch* deliberately preserves rare variants (compliance-safe)
- **Optimize — no-reingest maintenance** — a per-store dashboard (and `POST .../optimize`) that
  prunes the knowledge graph (low-confidence mentions + junk entities) and runs semantic dedup
  on what's already indexed: **dry-run first**, then apply, fully reversible — no re-ingestion
- **Per-model device** — place GLiNER/relex and Whisper independently on CPU or a specific GPU
  (`SOPHIA_VECTOR_GLINER_DEVICE` / `ASR_DEVICE`), with graceful CPU fallback
- **NLP utility endpoints** — the in-codebase models exposed on demand under `/v1/nlp/*`:
  `tokenize`/`detokenize` (BGE-M3), `ner` (GLiNER + regex), `classify` (GliClass), `relex` (typed
  relations), `transcribe` (Whisper). **Lazy-loaded** (zero cost when unused); the backend owns a
  **single copy** of each model and the ingestion worker calls them over HTTP — no duplicate weights
- **MCP server** — Sophia Vector as a **Model Context Protocol** server (`/mcp`, streamable-HTTP):
  agents get `search` (RAG) plus entity/classification/relation tools natively, reusing the same
  internal functions and models. Gated by `SOPHIA_VECTOR_MCP_ENABLED`, behind the same API key as `/v1/*`
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

| Category              | Formats                                                           | Handling                         |
|-----------------------|-------------------------------------------------------------------|----------------------------------|
| Documents             | PDF, DOCX, PPTX, XLSX, HTML/XHTML, Markdown, CSV, AsciiDoc, LaTeX | native (Docling)                 |
| Legacy / OpenDocument | DOC, PPT, XLS, RTF, ODT, ODP, ODS                                 | → OOXML via LibreOffice          |
| Email                 | EML, MSG                                                          | → HTML                           |
| Images                | PNG, JPG, TIFF, BMP, GIF, WEBP                                    | native, **OCR** for scanned text |
| Audio                 | WAV, MP3, M4A, OGG, FLAC, AAC, …                                  | → VTT via local Whisper          |
| Video                 | MP4, MOV, AVI, MKV, WEBM, …                                       | audio track → VTT via Whisper    |
| Subtitles             | VTT                                                               | native                           |

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

> ⚠️ **FalkorDB persistence — enable AOF or you lose the graph on restart.** FalkorDB is
> Redis-based: with the default config it only snapshots to its `/data` volume periodically, so a
> hard container restart can drop the graph (the RDB on disk lags behind memory). Turn on the
> append-only file so every write is durable — in `docker-compose.yml`, on the `falkordb` service:
> `environment: { REDIS_ARGS: "--appendonly yes --save 60 1000" }`. Qdrant is unaffected (separate
> persistence); and since the graph is *derived* data, if it's ever lost it rebuilds with a re-ingest.

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
docker buildx build --platform linux/amd64 -t sophiacloud/vector:0.6.0-alpha --push .
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
./compile-and-publish.sh 0.6.0-alpha cu130    # → sophiacloud/vector:0.6.0-alpha-cu130
```

Then point the compose service at the `-cu130` image, give it the GPU (host needs the NVIDIA driver

+ `nvidia-container-toolkit`), and opt the models in via env:

```yaml
# docker-compose.yml (sophia-vector service)
image: sophiacloud/vector:0.6.0-alpha-cu130
deploy:
  resources:
    reservations:
      devices: [ { driver: nvidia, count: 1, capabilities: [ gpu ] } ]
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

| Variable                                | Default                                       | Description                                                                     |
|-----------------------------------------|-----------------------------------------------|---------------------------------------------------------------------------------|
| `SOPHIA_VECTOR_QDRANT_URL`              | `http://localhost:6333`                       | Qdrant URL                                                                      |
| `SOPHIA_VECTOR_MONGODB_URI`             | `mongodb://localhost:27017/sophia_vector`     | Mongo connection (DB in the path)                                               |
| `SOPHIA_VECTOR_DOCLING_URL`             | `http://localhost:5001`                       | Docling parser URL                                                              |
| `SOPHIA_VECTOR_EMBEDDINGS_URL`          | `http://localhost:8004`                       | BGE-M3 embeddings + rerank URL                                                  |
| `SOPHIA_VECTOR_SECRET_KEY`              | —                                             | Fernet key, encrypts source secrets                                             |
| `SOPHIA_VECTOR_PARSER_MODEL_MAX_TOKENS` | `512`                                         | Chunk size in tokens (Docling)                                                  |
| `SOPHIA_VECTOR_PARSER_MAX_WAIT_SECONDS` | `36000`                                       | Per-doc parse timeout (≤ docling's max)                                         |
| `SOPHIA_VECTOR_PARSER_USE_OCR`          | `true`                                        | OCR scanned PDFs/images (force_ocr off)                                         |
| `SOPHIA_VECTOR_ASR_ENABLED`             | `true`                                        | Transcribe audio/video locally (Whisper)                                        |
| `SOPHIA_VECTOR_ASR_MODEL`               | `small`                                       | Whisper model (tiny…large-v3)                                                   |
| `SOPHIA_VECTOR_ASR_MAX_AUDIO_MINUTES`   | `60`                                          | Reject audio longer than this                                                   |
| `SOPHIA_VECTOR_ASR_MAX_VIDEO_MINUTES`   | `30`                                          | Reject video longer than this                                                   |
| `SOPHIA_VECTOR_GRAPH_ENABLED`           | `true`                                        | Enable the FalkorDB knowledge graph                                             |
| `SOPHIA_VECTOR_FALKOR_HOST`             | `localhost`                                   | FalkorDB host                                                                   |
| `SOPHIA_VECTOR_FALKOR_PASSWORD`         | `falkordb`                                    | FalkorDB password (`requirepass`)                                               |
| `SOPHIA_VECTOR_FALKOR_GRAPH_PREFIX`     | _(empty)_                                     | Graph-name namespace (multi-project)                                            |
| `SOPHIA_VECTOR_GLINER_ENABLED`          | `false`                                       | Zero-shot NER (opt-in; graph runs on regex when off)                            |
| `SOPHIA_VECTOR_GLINER_MODEL`            | `gliner-community/gliner_medium-v2.5`         | GLiNER NER model (multilingual, Apache)                                         |
| `SOPHIA_VECTOR_GLINER_LABELS`           | `autorità di vigilanza,organizzazione,…`      | Entity labels (CSV, zero-shot; banking default)                                 |
| `SOPHIA_VECTOR_GLINER_DEVICE`           | `cpu`                                         | GLiNER/relex device (`cpu`/`cuda`/`auto`)                                       |
| `SOPHIA_VECTOR_ASR_DEVICE`              | `cpu`                                         | Whisper device (`cpu`/`cuda`/`auto`)                                            |
| `SOPHIA_VECTOR_RELATIONS_ENABLED`       | `false`                                       | Typed relation extraction (GLiNER-relex)                                        |
| `SOPHIA_VECTOR_RELATIONS_LABELS`        | `pubblicato da,emesso da,…`                   | Relation labels (CSV, zero-shot default)                                        |
| `SOPHIA_VECTOR_CLASSIFIER_ENABLED`      | `false`                                       | Zero-shot chunk classification (GliClass; opt-in, needs `pip install gliclass`) |
| `SOPHIA_VECTOR_CLASSIFIER_LABELS`       | `antiriciclaggio,privacy e protezione dati,…` | Classification labels (CSV, zero-shot; by theme)                                |
| `SOPHIA_VECTOR_CURATION_ENABLED`        | `true`                                        | Boilerplate detection + suppression                                             |
| `SOPHIA_VECTOR_API_KEY`                 | _(empty)_                                     | Bearer key for `/v1/*` and `/mcp` (empty = no auth, trusted network)            |
| `SOPHIA_VECTOR_NLP_ENABLED`             | `true`                                        | Expose the `/v1/nlp/*` model endpoints                                          |
| `SOPHIA_VECTOR_MCP_ENABLED`             | `true`                                        | Expose the MCP server at `/mcp`                                                 |
| `SOPHIA_VECTOR_INTERNAL_API_URL`        | `http://127.0.0.1:8100`                       | Scheduler/worker → backend (internal)                                           |

## API Endpoints

```bash
# Vector stores
POST|GET /v1/vector_stores              GET|PATCH|DELETE /v1/vector_stores/{id}   # PATCH = rename
GET|DELETE /v1/vector_stores/{id}/files            # list / attach / remove files
POST /v1/vector_stores/{id}/search                 # multi-channel + graph-augmented search
GET|PUT /v1/vector_stores/{id}/schema              # extraction schema (store scope)
GET /v1/vector_stores/{id}/curation                # content-curation stats
POST /v1/vector_stores/{id}/optimize               # no-reingest maintenance (graph prune + semantic dedup)
GET /v1/vector_stores/{id}/graph                   # export knowledge graph (force-graph viewer)

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

# NLP utilities — the in-codebase models as an on-demand API (lazy-loaded)
POST /v1/nlp/tokenize    POST /v1/nlp/detokenize          # BGE-M3 tokenizer
POST /v1/nlp/ner    POST /v1/nlp/classify    POST /v1/nlp/relex   # GLiNER / GliClass / GLiNER-relex
POST /v1/nlp/transcribe                            # audio/video → VTT (Whisper)

# MCP server (Model Context Protocol, streamable-HTTP) — tools for MCP-compatible agents
GET|POST /mcp   # search, list_vector_stores, list_directories, extract_entities, classify_text, extract_relations
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
Explore it from the built-in **force-graph viewer** (`/stores/{id}/graph`, 2D/3D), the FalkorDB UI,
or via Cypher, e.g.:

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

### Gold-set audit (real corpus)

`scripts/audit_ingest.py` + `scripts/audit_eval.py` ingest a real document sample via the API and
score the search against a labeled gold set — **positives** (query → expected document) and
**negatives** (out-of-corpus queries that must return *nothing*). On a 30-document banking sample
(26 positives + 8 negatives):

| rerank threshold | recall@5 | MRR  | out-of-corpus leakage |
|------------------|---------:|-----:|----------------------:|
| 0.10             | 92%      | 0.84 | 4/8 ❌                 |
| **0.25 (default)** | **88%**  | 0.80 | **0/8 ✅**             |
| 0.50             | 81%      | 0.77 | 0/8 ✅                 |

The default `score_threshold` is **0.25**: it keeps recall high while the search stays **surgical**
— only relevant hits, and **empty when the answer isn't there** (negatives top out at ~0.20, well
below the cutoff). Graph expansion and NER do **not** change retrieval recall here (dense+sparse+
rerank already saturate it); they add faceting / graph / extraction value, not retrieval lift.

## API Documentation

Interactive API reference (Scalar, shown above) — full `/v1/*` surface, try-it-out, MCP export:

- API reference (Scalar): `http://localhost:8100/docs`
- Raw OpenAPI schema: `http://localhost:8100/openapi.json`

## Version

Current version: **0.6.0-alpha**

## License

Service code, API, and orchestration logic are licensed under the
**PolyForm Noncommercial License 1.0.0**. © Sophia AI Cloud — https://www.sophia-cloud.com
