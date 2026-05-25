# tests — verifica formati supportati

Suite di verifica dei formati che Sophia Vector è in grado di ingerire, end-to-end
contro il parser Docling reale. Serve a **dichiarare** (e ri-verificare a ogni
modifica) cosa la pipeline accetta e come.

## Contenuto

- `fixtures/` — un file di esempio per ogni formato dichiarato (stesso contenuto:
  keyword + IBAN + una tabella), così gli esiti sono confrontabili.
- `gen_fixtures.py` — rigenera le fixture in modo riproducibile (LibreOffice +
  ImageMagick + stdlib). I sorgenti testuali sono inline nello script.
- `verify_formats.py` — passa ogni fixture per `normalize_for_parser()` +
  `upload_file_for_chunking_sync()` (Docling) e stampa una tabella PASS/FAIL;
  a fine run aggiorna `SUPPORTED_FORMATS.md`.
- `SUPPORTED_FORMATS.md` — la lista dichiarabile, generata dall'ultima verifica.
- `docling_chunking.py` / `docling_parsing.py` — script di debug *single-file*
  preesistenti (mandano un file a Docling e mostrano chunk / markdown). Utili per
  ispezionare un singolo documento; la verifica multi-formato è `verify_formats.py`.

## Uso

```bash
# 1) (opzionale) rigenera le fixture
.venv/bin/python tests/gen_fixtures.py

# 2) verifica tutto contro Docling (dev: ./dev-start.sh + servizi compose su)
.venv/bin/python tests/verify_formats.py
```

Richiede Docling raggiungibile (`SOPHIA_VECTOR_DOCLING_URL`, default `:5001`) e,
per i formati convertiti, LibreOffice + `extract-msg` + `ffmpeg`/`faster-whisper`
(tutti presenti nell'immagine). Suggerito `SOPHIA_VECTOR_ASR_MODEL=tiny` per una
verifica veloce (il default applicativo è `small`).

## Note

- **Immagini**: accettate, ma l'estrazione testo richiede
  `SOPHIA_VECTOR_PARSER_USE_OCR=true` (in dev è off → 0 chunk, esito comunque OK).
- **`.msg` (Outlook)**: supportato via `extract-msg`, ma non è auto-generabile
  senza MAPI. Per includerlo nella verifica, copia un `.msg` reale in
  `fixtures/sample.msg` e rilancia `verify_formats.py`.
- **Audio/Video**: trascritti in casa con faster-whisper (lazy, CPU) → VTT, poi
  chunkati da Docling. Le fixture `sample.wav`/`sample.mp4` sono **toni** (no
  parlato) → VTT vuoto: provano estrazione + routing, non la qualità di
  trascrizione. Per un test con contenuto reale, metti un audio parlato in
  `fixtures/` e rilancia. Limiti durata: 60 min audio / 30 min video (env).
