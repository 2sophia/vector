# Changelog

All notable changes to Sophia Vector are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/); the project follows
semantic versioning (currently in the `alpha` pre-release line).

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
