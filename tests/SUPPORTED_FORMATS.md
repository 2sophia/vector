# Formati supportati — Sophia Vector

Generato da `tests/verify_formats.py`. La **whitelist completa** qui sotto è la lista autorevole (derivata dai set in `utils/docling.py`): è ciò che i validate accettano in upload e nel fail-fast SharePoint. La **tabella esiti** più in basso copre solo i formati con una fixture realmente passata per la pipeline (normalizzazione pre-parser + chunking Docling reale).

## Whitelist completa (autorevole — da `utils/docling.py`)

**48 estensioni** accettate dai validate (upload manuale + fail-fast SharePoint).

**Nativi** — Docling li parsa direttamente:

> `.adoc`  `.asciidoc`  `.bmp`  `.csv`  `.docx`  `.gif`  `.htm`  `.html`  `.jpeg`  `.jpg`  `.md`  `.pdf`  `.png`  `.pptx`  `.tex`  `.tif`  `.tiff`  `.vtt`  `.webp`  `.xhtml`  `.xlsx`

**Convertiti pre-parser** — passano da `utils/convert.py` prima di Docling:

| Estensione | → target | Come |
|---|---|---|
| `.eml` | `html` | stdlib `email` |
| `.msg` | `html` | `extract-msg` |
| `.doc` `.odt` `.rtf` | `docx` | LibreOffice |
| `.odp` `.ppt` | `pptx` | LibreOffice |
| `.ods` `.xls` | `xlsx` | LibreOffice |
| `.odg` | `pdf` | LibreOffice (Draw) |
| `.txt` | `md` | copia diretta |

**Audio** — trascritti in casa (faster-whisper) → `.vtt`, se `ASR_ENABLED`:

> `.aac`  `.flac`  `.m4a`  `.mp3`  `.ogg`  `.opus`  `.wav`  `.wma`

**Video** — audio estratto + trascritto → `.vtt`, se `ASR_ENABLED`:

> `.avi`  `.flv`  `.m4v`  `.mkv`  `.mov`  `.mp4`  `.webm`  `.wmv`

## Esiti pipeline sulle fixture

I formati **nativi** li parsa Docling direttamente; i **convertiti** passano da `utils/convert.py` prima del parser. `.msg` non è auto-generabile (serve MAPI) → testato solo se aggiungi a mano `tests/fixtures/sample.msg`.

| Estensione | Tipo | Conversione | Chunk | Esito | Note |
|---|---|---|---:|:---:|---|
| `.adoc` | nativo | — | 2 | ✅ |  |
| `.bmp` | nativo | — | 3 | ✅ |  |
| `.csv` | tabulare | — | 2 | ✅ |  |
| `.doc` | convertito | → .docx | 2 | ✅ |  |
| `.docx` | nativo | — | 2 | ✅ |  |
| `.eml` | convertito | → .html | 4 | ✅ |  |
| `.gif` | nativo | — | 3 | ✅ |  |
| `.html` | nativo | — | 2 | ✅ |  |
| `.jpg` | nativo | — | 3 | ✅ |  |
| `.md` | nativo | — | 2 | ✅ |  |
| `.mp4` | convertito | → .vtt | 0 | ✅ | tono di test (no parlato) → vuoto |
| `.odg` | convertito | → .pdf | 1 | ✅ |  |
| `.odp` | convertito | → .pptx | 3 | ✅ |  |
| `.ods` | tabulare | → .xlsx | 2 | ✅ |  |
| `.odt` | convertito | → .docx | 2 | ✅ |  |
| `.pdf` | nativo | — | 2 | ✅ |  |
| `.png` | nativo | — | 3 | ✅ |  |
| `.ppt` | convertito | → .pptx | 3 | ✅ |  |
| `.pptx` | nativo | — | 3 | ✅ |  |
| `.rtf` | convertito | → .docx | 2 | ✅ |  |
| `.tex` | nativo | — | 1 | ✅ |  |
| `.tiff` | nativo | — | 3 | ✅ |  |
| `.txt` | convertito | → .md | 2 | ✅ |  |
| `.vtt` | nativo | — | 2 | ✅ |  |
| `.wav` | convertito | → .vtt | 0 | ✅ | tono di test (no parlato) → vuoto |
| `.webp` | nativo | — | 3 | ✅ |  |
| `.xls` | tabulare | → .xlsx | 2 | ✅ |  |
| `.xlsx` | tabulare | — | 2 | ✅ |  |

**Note generali**
- Immagini (`.png/.jpg/.tiff/.bmp/.gif/.webp`): OCR attivo di default (`do_ocr=True`); `force_ocr` resta `False` così i PDF col text layer non vengono ri-OCR-ati (force_ocr farebbe "solo OCR" su tutto).
- `.msg` (Outlook): supportato via `extract-msg`; il sample va aggiunto a mano in `tests/fixtures/sample.msg` (non auto-generabile senza MAPI).
- I formati Office binari/ODF/RTF richiedono **LibreOffice** nell'immagine; `.eml`/`.txt` no (stdlib).
- **Tabellari** (`.csv`/`.xlsx`, e `.xls`/`.ods` dopo conversione) non passano da Docling ma dal **chunker tabulare** (`utils/tabular.py`): una *table card* (schema + statistiche per colonna + righe campione) e le righe verbalizzate fino a un cap. Evita l'esplosione di Docling sulle tabelle enormi; il numero di chunk dipende dalle righe. Spegnibile con `SOPHIA_VECTOR_TABULAR_ENABLED=false`.
- Alias coperti dallo stesso path (senza fixture dedicata): `.htm`/`.xhtml` (= html), `.jpeg` (= jpg), `.tif` (= tiff), `.asciidoc` (= adoc).
- **Audio/Video**: trascritti **in casa** con faster-whisper (lazy, CPU) → VTT, poi chunkati da Docling — **non** dipende dalla config del parser. Attivi se `ASR_ENABLED` (default on); il modello si carica al primo file e resta caldo. Le fixture sono toni di test (nessun parlato) → 0 chunk; con audio parlato producono testo + timestamp. Limiti durata: **60 min audio / 30 min video** (`SOPHIA_VECTOR_ASR_MAX_AUDIO_MINUTES` / `_VIDEO_MINUTES`, tunabili).
- **XML specializzati** (`xml_uspto/xml_jats/xml_xbrl/mets_gbs/json_docling`): nell'enum del parser ma non abilitati (nessun caso d'uso bancario per ora).
