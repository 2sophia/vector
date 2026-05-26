# Changelog

All notable changes to Sophia Vector are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/); the project follows
semantic versioning (currently in the `alpha` pre-release line).

## [0.4.0-alpha] — 2026-05-26

A large quality + product pass. The heavy ML layers become **opt-in** so the default install is
light and predictable, while the knowledge graph stays on out-of-the-box (populated by
high-precision regex). Retrieval, entity extraction and the maintenance tooling all get sharper,
and a zero-shot classifier is wired in for faceting.

### Added
- **High-precision regex extraction** (`models/ner.py`): universal patterns (email, URL,
  IBAN/ISIN, amounts, percentages, dates) plus Italian legal references (D.Lgs/D.M., Legge,
  Regolamento UE incl. delegato/di esecuzione, Direttiva, Decisione UE/PESC, Delibera,
  Provvedimento, Circolare, EBA/GL codes, GDPR/TUB/TUF article refs). Variants collapse onto the
  same graph node. The graph is now meaningful **without** the heavy zero-shot model.
- **Zero-shot chunk classification** (`models/classifier.py`, GliClass, opt-in): tags chunks by
  theme/sensitivity → `payload.category` for faceting/filtering. `pip install gliclass` to enable.
- **Optimize — no-reingest maintenance** (`POST /v1/vector_stores/{id}/optimize` + per-store UI):
  graph prune (low-confidence mentions + junk entities) and **semantic dedup** of near-duplicates
  via dense∩sparse agreement (marked, not deleted; fully reversible). Dry-run first.
- **Knowledge-graph viewer** (`/stores/{id}/graph`, force-graph 2D/3D) plus `GET …/graph` export.
- **Rename vector store** (`PATCH /v1/vector_stores/{id}`) and **rename directory**, inline from
  the UI. A shadcn/radix `Select`, a collapsible extraction panel, and an "add current folder"
  action in the source browser.
- **Rich startup banner** (`utils/banner.py`): services + ingestion models (NER / relations /
  classifier / ASR) with device and on/off state.

### Changed
- **Default posture: heavy ML is opt-in.** Zero-shot NER (GLiNER), typed relations and the
  classifier are **off by default**; `GRAPH_ENABLED` stays **on**, populated by regex (light, no
  model download). The dense+sparse+rerank core is always on; dev enables everything via `.env`.
- **GLiNER model → `gliner-community/gliner_medium-v2.5`** (Apache-2.0, was CC-BY-NC). Default
  entity labels tuned for the banking/legal domain (`autorità di vigilanza`, `organo aziendale`,
  …); classifier labels by **theme**, not document type (overlapping doc-types don't discriminate).
- **Rerank score threshold 0.22 → 0.1.** The absolute cross-encoder cutoff was zeroing legitimate
  paraphrase/cross-lingual queries (good dense match, low rerank score); 0.1 recovers them while
  still dropping noise — in-corpus ranking is unchanged.
- **Docling memory mitigation** (`SOPHIA_VECTOR_DOCLING_CLEAR_EVERY`, default 20): the ingestion
  worker clears the parser caches every N docs and at end of batch.
- Blocking calls inside `async def` endpoints moved off the event loop (`asyncio.to_thread`).

### Fixed
- **Optimize "Reset marcatura"** no longer runs the destructive graph cleanup as a side effect.

## [0.3.0-alpha] — 2026-05-25

Retrieval and the knowledge graph get a substantial quality pass: search now fuses multiple
channels with RRF, a configurable zero-shot extraction schema cascades across scopes, typed
relations enrich the graph, and a content-curation layer suppresses boilerplate. Everything is
additive, flag-gated, and leaves the Qdrant payload contract untouched.

### Added
- **Multi-channel search + RRF** (`utils/fusion.py`, `routers/search.py`): the dense+sparse
  vector channel is fused with an optional graph channel and a **lexical/exact-match channel**
  (Qdrant full-text `MatchText` over the `text` payload index) via Reciprocal Rank Fusion, then
  a single cross-encoder rerank. The lexical channel catches codes and references the dense
  retriever misses (e.g. `D.lgs 231/2001`, `204/26`).
- **Configurable extraction schema, cascading** (`utils/store_schema.py`): the GLiNER /
  GLiNER-relex engine is zero-shot, so *what* to extract is data. Entity labels, relation
  labels and the relations toggle resolve per-field through **file → directory → sync → store →
  global default**. `GET/PUT(/DELETE) …/schema` on stores, directories and sync sources, with a
  reusable `<SchemaEditor>` panel on all three levels in the frontend.
- **Typed knowledge-graph relations** (`utils/relations.py`): GLiNER-relex
  (`knowledgator/gliner-relex-multi-v1.0`) extracts typed `:REL {type,score}` edges between
  entities (e.g. `DECRETO —emesso da→ PRESIDENTE DELLA REPUBBLICA`), joint zero-shot NER+RE, no
  LLM. Agnostic (labels from the schema), off by default, with agnostic hygiene filters.
- **Content curation / boilerplate suppression** (`utils/curation.py`): a per-collection
  body-hash (content with heading prefixes stripped) detects near-duplicate chunks shared across
  many documents and suppresses them at search time; the shared content is also linked in the
  graph (`:Content` via `:SAME_CONTENT`). `GET /v1/vector_stores/{id}/curation` reports stats.
- **Per-model device selection** (`utils/device.py`): `SOPHIA_VECTOR_GLINER_DEVICE` and
  `SOPHIA_VECTOR_ASR_DEVICE` (`cpu|cuda|cuda:N|auto`, default `cpu`) place GLiNER/relex and
  Whisper independently on CPU or a specific GPU, with graceful fallback when CUDA is absent.
- **Pipeline inspector** (`scripts/verify_pipeline.py`): dumps job status, Qdrant points, graph
  nodes/edges, curation stats and an optional live search for a given vector store.

### Changed
- **Broken-file guard** in the ingestion worker: a zero-byte/missing file fails fast as
  `FAILED`; a file that parses to **zero chunks** is now `FAILED` instead of a silent `COMPLETED`.

## [0.2.0-alpha] — 2026-05-25

Ingestion now accepts a much wider range of formats, with everything Docling can't read
natively normalized by an in-process pre-parser layer — no GPU, no extra services.

### Added
- **Wide format coverage** via a pre-parser conversion layer (`utils/convert.py`):
  - email — `.eml` (stdlib) and `.msg` (extract-msg) → HTML
  - legacy Office `.doc/.ppt/.xls`, `.rtf`, OpenDocument `.odt/.ods/.odp` → OOXML via LibreOffice
  - `.txt` → Markdown
- **OCR on by default** (`SOPHIA_VECTOR_PARSER_USE_OCR=true`): scanned PDFs and images are
  indexed. `force_ocr` stays off, so native text layers are not re-OCR'd.
- **LaTeX (`.tex`) and WebVTT (`.vtt`)** as native formats.
- **Audio/Video transcription** (`utils/transcribe.py`): local faster-whisper on CPU,
  lazy-loaded like GLiNER; output to VTT, then chunked through the normal pipeline. ffmpeg
  extracts the audio track from video. Gated by `SOPHIA_VECTOR_ASR_ENABLED` with duration
  caps (`ASR_MAX_AUDIO_MINUTES`=60, `ASR_MAX_VIDEO_MINUTES`=30, tunable).
- **Single source of truth for accepted formats**: `GET /v1/files/supported-formats`,
  consumed by manual upload, SharePoint ingestion and the frontend file picker.
- **Format verification suite** (`tests/`): fixtures generator, an end-to-end verifier against
  a live Docling, and a generated `tests/SUPPORTED_FORMATS.md`.

### Changed
- File upload (`POST /v1/files`) now validates the extension against the shared whitelist
  (previously size-only) — unsupported files fail fast with `415` instead of a dead job later.
- Removed the deprecated `ocr_engine` request parameter (Docling defaults to `auto`).

### Ops
- The Docker image now bundles **LibreOffice**, **ffmpeg** and **faster-whisper**, so it is
  noticeably larger. The Whisper model is downloaded on the first audio/video file and then
  cached — ensure the container can reach Hugging Face, or pre-cache the model.

## [0.1.0-alpha] — 2026-05-24

### Added
- First public release: OpenAI-compatible vector store on Qdrant, hybrid search with BGE-M3
  cross-encoder reranking, a FalkorDB knowledge graph with graph-augmented retrieval, GLiNER +
  regex entity extraction (no LLM in ingestion), multi-source ingestion (SharePoint), internal
  scheduled sync, and a Next.js management frontend.
