# Formati supportati — Sophia Vector

Generato da `tests/verify_formats.py` (pipeline reale: normalizzazione pre-parser + chunking Docling). I formati **nativi** li parsa Docling direttamente; i **convertiti** passano da `utils/convert.py` prima del parser.

| Estensione | Tipo | Conversione | Chunk | Esito | Note |
|---|---|---|---:|:---:|---|
| `.adoc` | nativo | — | 2 | ✅ |  |
| `.bmp` | nativo | — | 3 | ✅ |  |
| `.csv` | nativo | — | 1 | ✅ |  |
| `.eml` | convertito | → .html | 4 | ✅ |  |
| `.gif` | nativo | — | 3 | ✅ |  |
| `.html` | nativo | — | 2 | ✅ |  |
| `.jpg` | nativo | — | 3 | ✅ |  |
| `.md` | nativo | — | 2 | ✅ |  |
| `.mp4` | convertito | → .vtt | 0 | ✅ | tono di test (no parlato) → vuoto |
| `.odp` | convertito | → .pptx | 3 | ✅ |  |
| `.ods` | convertito | → .xlsx | 1 | ✅ |  |
| `.odt` | convertito | → .docx | 2 | ✅ |  |
| `.pdf` | nativo | — | 2 | ✅ |  |
| `.png` | nativo | — | 3 | ✅ |  |
| `.ppt` | convertito | → .pptx | 3 | ✅ |  |
| `.pptx` | nativo | — | 3 | ✅ |  |
| `.tex` | nativo | — | 1 | ✅ |  |
| `.tiff` | nativo | — | 3 | ✅ |  |
| `.txt` | convertito | → .md | 2 | ✅ |  |
| `.vtt` | nativo | — | 2 | ✅ |  |
| `.wav` | convertito | → .vtt | 0 | ✅ | tono di test (no parlato) → vuoto |
| `.webp` | nativo | — | 3 | ✅ |  |
| `.xls` | convertito | → .xlsx | 1 | ✅ |  |
| `.xlsx` | nativo | — | 1 | ✅ |  |

**Note generali**
- Immagini (`.png/.jpg/.tiff/.bmp/.gif/.webp`): OCR attivo di default (`do_ocr=True`); `force_ocr` resta `False` così i PDF col text layer non vengono ri-OCR-ati (force_ocr farebbe "solo OCR" su tutto).
- `.msg` (Outlook): supportato via `extract-msg`; il sample va aggiunto a mano in `tests/fixtures/sample.msg` (non auto-generabile senza MAPI).
- I formati Office binari/ODF/RTF richiedono **LibreOffice** nell'immagine; `.eml`/`.txt` no (stdlib).
- Alias coperti dallo stesso path (senza fixture dedicata): `.htm`/`.xhtml` (= html), `.jpeg` (= jpg), `.tif` (= tiff), `.asciidoc` (= adoc).
- **Audio/Video**: trascritti **in casa** con faster-whisper (lazy, CPU) → VTT, poi chunkati da Docling — **non** dipende dalla config del parser. Attivi se `ASR_ENABLED` (default on); il modello si carica al primo file e resta caldo. Le fixture sono toni di test (nessun parlato) → 0 chunk; con audio parlato producono testo + timestamp. Limiti durata: **60 min audio / 30 min video** (`SOPHIA_VECTOR_ASR_MAX_AUDIO_MINUTES` / `_VIDEO_MINUTES`, tunabili).
- **XML specializzati** (`xml_uspto/xml_jats/xml_xbrl/mets_gbs/json_docling`): nell'enum del parser ma non abilitati (nessun caso d'uso bancario per ora).
