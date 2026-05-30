"""
Verifica end-to-end di TUTTI i formati supportati, contro Docling reale.

Per ogni file in tests/fixtures/ esegue l'intera pipeline pre-parser:
  normalize_for_parser()  →  upload_file_for_chunking_sync()  (Docling)
e riporta classificazione (nativo/convertito), eventuale conversione, numero di
chunk e esito. A fine run scrive la lista dichiarabile in SUPPORTED_FORMATS.md.

Uso:  .venv/bin/python tests/verify_formats.py
Richiede: Docling raggiungibile (SOPHIA_VECTOR_DOCLING_URL, default :5001) e,
per i formati convertiti, LibreOffice + extract-msg disponibili (come in immagine).
"""

import os
import sys
import shutil
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.INFO)  # silenzia i log debug del client docling

from utils.convert import normalize_for_parser, UnsupportedFormatError, _OFFICE_TARGET  # noqa: E402
from utils.docling import (  # noqa: E402
    upload_file_for_chunking_sync,
    PARSER_NATIVE_EXTENSIONS,
    PARSER_CONVERTIBLE_EXTENSIONS,
    ASR_AUDIO_EXTENSIONS,
    ASR_VIDEO_EXTENSIONS,
)
from utils.tabular import is_tabular, chunk_tabular  # noqa: E402
from utils.settings import TABULAR_ENABLED  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
OUT_MD = os.path.join(os.path.dirname(__file__), "SUPPORTED_FORMATS.md")


def classify(ext: str) -> str:
    if ext in PARSER_NATIVE_EXTENSIONS:
        return "nativo"
    if ext in PARSER_CONVERTIBLE_EXTENSIONS:
        return "convertito"
    return "non supportato"


def whitelist_lines() -> list:
    """Sezione AUTOREVOLE: ogni estensione accettata, derivata dai set di
    `utils/docling.py` (+ target di conversione da `utils/convert._OFFICE_TARGET`).

    Si genera dal codice, non dalle fixture → non può andare fuori sync con ciò
    che i validate accettano davvero. La tabella esiti più sotto è solo il
    sottoinsieme con una fixture passata per Docling reale."""
    # convertibili con mezzo di conversione "speciale" (non LibreOffice)
    special = {
        ".eml": ("html", "stdlib `email`"),
        ".msg": ("html", "`extract-msg`"),
        ".txt": ("md", "copia diretta"),
    }
    how = {"docx": "LibreOffice", "pptx": "LibreOffice",
           "xlsx": "LibreOffice", "pdf": "LibreOffice (Draw)"}

    native = sorted(PARSER_NATIVE_EXTENSIONS)
    audio = sorted(ASR_AUDIO_EXTENSIONS)
    video = sorted(ASR_VIDEO_EXTENSIONS)
    conv = sorted(PARSER_CONVERTIBLE_EXTENSIONS - ASR_AUDIO_EXTENSIONS - ASR_VIDEO_EXTENSIONS)
    total = len(native) + len(conv) + len(audio) + len(video)

    # raggruppa i convertibili per (target, come) così doc/rtf/odt stanno in una riga
    groups: dict = {}
    for e in conv:
        if e in special:
            key = special[e]
        else:
            tgt = _OFFICE_TARGET.get(e, "?")
            key = (tgt, how.get(tgt, "—"))
        groups.setdefault(key, []).append(e)

    order = ["html", "docx", "pptx", "xlsx", "pdf", "md"]
    def _k(item):
        (tgt, _note), _exts = item
        return (order.index(tgt) if tgt in order else 99, tgt)

    L = [
        "## Whitelist completa (autorevole — da `utils/docling.py`)",
        "",
        f"**{total} estensioni** accettate dai validate (upload manuale + fail-fast SharePoint).",
        "",
        "**Nativi** — Docling li parsa direttamente:",
        "",
        "> " + "  ".join(f"`{e}`" for e in native),
        "",
        "**Convertiti pre-parser** — passano da `utils/convert.py` prima di Docling:",
        "",
        "| Estensione | → target | Come |",
        "|---|---|---|",
    ]
    for (tgt, note), exts in sorted(groups.items(), key=_k):
        L.append(f"| {' '.join(f'`{e}`' for e in sorted(exts))} | `{tgt}` | {note} |")
    L += [
        "",
        "**Audio** — trascritti in casa (faster-whisper) → `.vtt`, se `ASR_ENABLED`:",
        "",
        "> " + "  ".join(f"`{e}`" for e in audio),
        "",
        "**Video** — audio estratto + trascritto → `.vtt`, se `ASR_ENABLED`:",
        "",
        "> " + "  ".join(f"`{e}`" for e in video),
    ]
    return L


def run_one(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    row = {"ext": ext, "kind": classify(ext), "via": "—", "chunks": 0, "status": "?", "note": ""}
    tmp = None
    try:
        parse_path, tmp = normalize_for_parser(path)
        if tmp:
            row["via"] = os.path.splitext(parse_path)[1].lower()
        # csv/xlsx → chunker tabulare dedicato (come fa il worker), il resto → Docling
        if TABULAR_ENABLED and is_tabular(parse_path):
            res = chunk_tabular(parse_path)
            row["kind"] = "tabulare"
        else:
            res = upload_file_for_chunking_sync(parse_path)
        chunks = res.get("chunks") or []
        row["chunks"] = len(chunks)
        row["status"] = "OK"
        if not chunks:
            if ext in ASR_AUDIO_EXTENSIONS or ext in ASR_VIDEO_EXTENSIONS:
                row["note"] = "tono di test (no parlato) → vuoto"
            else:
                row["note"] = "0 chunk (contenuto vuoto?)"
    except UnsupportedFormatError:
        row["status"] = "RIFIUTATO"
        row["note"] = "estensione non in whitelist"
    except Exception as e:
        row["status"] = "FAIL"
        row["note"] = str(e)[:80]
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return row


def main():
    files = sorted(f for f in os.listdir(FIX) if not f.startswith("."))
    if not files:
        print("Nessuna fixture. Esegui prima: .venv/bin/python tests/gen_fixtures.py")
        return

    rows = [run_one(os.path.join(FIX, f)) for f in files]

    # tabella a console
    w = max(len(f) for f in files)
    print(f"\n{'file'.ljust(w)}  {'tipo':<11} {'via':<6} {'chunk':>5}  esito")
    print("-" * (w + 34))
    ok = 0
    for f, r in zip(files, rows):
        ok += r["status"] == "OK"
        line = f"{f.ljust(w)}  {r['kind']:<11} {r['via']:<6} {r['chunks']:>5}  {r['status']}"
        if r["note"]:
            line += f"  ({r['note']})"
        print(line)
    print("-" * (w + 34))
    print(f"{ok}/{len(rows)} OK\n")

    # markdown dichiarabile
    icon = {"OK": "✅", "FAIL": "❌", "RIFIUTATO": "⛔"}
    lines = [
        "# Formati supportati — Sophia Vector",
        "",
        "Generato da `tests/verify_formats.py`. La **whitelist completa** qui sotto è la "
        "lista autorevole (derivata dai set in `utils/docling.py`): è ciò che i validate "
        "accettano in upload e nel fail-fast SharePoint. La **tabella esiti** più in basso "
        "copre solo i formati con una fixture realmente passata per la pipeline "
        "(normalizzazione pre-parser + chunking Docling reale).",
        "",
    ]
    lines += whitelist_lines()
    lines += [
        "",
        "## Esiti pipeline sulle fixture",
        "",
        "I formati **nativi** li parsa Docling direttamente; i **convertiti** passano da "
        "`utils/convert.py` prima del parser. `.msg` non è auto-generabile (serve MAPI) → "
        "testato solo se aggiungi a mano `tests/fixtures/sample.msg`.",
        "",
        "| Estensione | Tipo | Conversione | Chunk | Esito | Note |",
        "|---|---|---|---:|:---:|---|",
    ]
    for f, r in zip(files, rows):
        via = "—" if r["via"] == "—" else f"→ {r['via']}"
        lines.append(
            f"| `{r['ext']}` | {r['kind']} | {via} | {r['chunks']} | "
            f"{icon.get(r['status'], r['status'])} | {r['note']} |"
        )
    lines += [
        "",
        "**Note generali**",
        "- Immagini (`.png/.jpg/.tiff/.bmp/.gif/.webp`): OCR attivo di default "
        "(`do_ocr=True`); `force_ocr` resta `False` così i PDF col text layer non "
        "vengono ri-OCR-ati (force_ocr farebbe \"solo OCR\" su tutto).",
        "- `.msg` (Outlook): supportato via `extract-msg`; il sample va aggiunto a "
        "mano in `tests/fixtures/sample.msg` (non auto-generabile senza MAPI).",
        "- I formati Office binari/ODF/RTF richiedono **LibreOffice** nell'immagine; "
        "`.eml`/`.txt` no (stdlib).",
        "- **Tabellari** (`.csv`/`.xlsx`, e `.xls`/`.ods` dopo conversione) non passano "
        "da Docling ma dal **chunker tabulare** (`utils/tabular.py`): una *table card* "
        "(schema + statistiche per colonna + righe campione) e le righe verbalizzate "
        "fino a un cap. Evita l'esplosione di Docling sulle tabelle enormi; il numero "
        "di chunk dipende dalle righe. Spegnibile con `SOPHIA_VECTOR_TABULAR_ENABLED=false`.",
        "- Alias coperti dallo stesso path (senza fixture dedicata): `.htm`/`.xhtml` "
        "(= html), `.jpeg` (= jpg), `.tif` (= tiff), `.asciidoc` (= adoc).",
        "- **Audio/Video**: trascritti **in casa** con faster-whisper (lazy, CPU) → "
        "VTT, poi chunkati da Docling — **non** dipende dalla config del parser. Attivi "
        "se `ASR_ENABLED` (default on); il modello si carica al primo file e resta caldo. "
        "Le fixture sono toni di test (nessun parlato) → 0 chunk; con audio parlato "
        "producono testo + timestamp. Limiti durata: **60 min audio / 30 min video** "
        "(`SOPHIA_VECTOR_ASR_MAX_AUDIO_MINUTES` / `_VIDEO_MINUTES`, tunabili).",
        "- **XML specializzati** (`xml_uspto/xml_jats/xml_xbrl/mets_gbs/json_docling`): "
        "nell'enum del parser ma non abilitati (nessun caso d'uso bancario per ora).",
    ]
    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"📄 lista dichiarabile aggiornata: {OUT_MD}")


if __name__ == "__main__":
    main()
