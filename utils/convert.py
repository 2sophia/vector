"""
Pre-parser normalization layer.

Docling mangia nativamente solo un set di formati (PDF, OOXML 2007+, HTML, MD,
CSV, AsciiDoc, immagini). Per i formati che NON gestisce — mail (.eml/.msg),
Office binario 97-2003 (.doc/.ppt/.xls), .rtf, ODF (.odt/.ods/.odp), .txt — li
convertiamo QUI in un formato che Docling capisce, *prima* di mandarli al parser.

Imbuto unico: chiamato da `workers.vector.handle_job` subito prima di
`upload_file_for_chunking_sync`, così copre sia l'upload manuale che l'ingest
SharePoint senza dover replicare la logica nei vari validate (a quelli basta la
whitelist condivisa `PARSER_SUPPORTED_EXTENSIONS`).

Contratto: `normalize_for_parser(path) -> (parse_path, tmp_dir)`.
- formato già nativo  → (path invariato, None)   [niente da pulire]
- formato convertibile → (path del file convertito in una tempdir, tmp_dir)
  il chiamante DEVE fare `shutil.rmtree(tmp_dir)` quando ha finito col parser.
- formato non supportato → solleva `UnsupportedFormatError`.

Ogni conversione è seguita da un check "non è rotto" (output esiste e non vuoto):
se fallisce solleva, e il worker manda il job in FAILED (niente loop infinito).
"""

import os
import html
import shutil
import logging
import tempfile
import subprocess

logger = logging.getLogger("convert")

# LibreOffice headless: timeout per singola conversione. Office binario denso
# può metterci un po', ma se sfora è quasi sempre un file rotto/appeso.
SOFFICE_TIMEOUT_SECONDS = 180

# Mappa estensione sorgente → formato target LibreOffice (uno che Docling mangia).
_OFFICE_TARGET = {
    ".doc": "docx", ".rtf": "docx", ".odt": "docx",
    ".ppt": "pptx", ".odp": "pptx",
    ".xls": "xlsx", ".ods": "xlsx",
}


class UnsupportedFormatError(Exception):
    """Estensione né nativa né convertibile."""


def _new_tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="svconv_")


# ---------------------------------------------------------------------------
# Mail: .eml (stdlib) e .msg (extract-msg) → HTML
# ---------------------------------------------------------------------------

def _wrap_email_html(subject: str, sender: str, to: str, date: str, body_html: str) -> str:
    """Avvolge il corpo mail con un'intestazione leggibile (resa nel chunking)."""
    head = (
        f"<p><b>Oggetto:</b> {html.escape(subject)}<br>"
        f"<b>Da:</b> {html.escape(sender)}<br>"
        f"<b>A:</b> {html.escape(to)}<br>"
        f"<b>Data:</b> {html.escape(date)}</p><hr>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(subject) or 'Email'}</title></head>"
        f"<body><h1>{html.escape(subject)}</h1>{head}{body_html}</body></html>"
    )


def _eml_to_html(src: str, out_path: str) -> None:
    from email import policy
    from email.parser import BytesParser

    with open(src, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    subject = str(msg["subject"] or "")
    sender = str(msg["from"] or "")
    to = str(msg["to"] or "")
    date = str(msg["date"] or "")

    body_part = msg.get_body(preferencelist=("html", "plain"))
    if body_part is None:
        body_html = ""
    else:
        content = body_part.get_content()
        if body_part.get_content_type() == "text/plain":
            body_html = "<pre>" + html.escape(content) + "</pre>"
        else:
            body_html = content  # già HTML

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_wrap_email_html(subject, sender, to, date, body_html))


def _msg_to_html(src: str, out_path: str) -> None:
    import extract_msg

    msg = extract_msg.Message(src)
    try:
        subject = str(getattr(msg, "subject", "") or "")
        sender = str(getattr(msg, "sender", "") or "")
        to = str(getattr(msg, "to", "") or "")
        date = str(getattr(msg, "date", "") or "")

        raw_html = getattr(msg, "htmlBody", None)
        if raw_html:
            body_html = raw_html.decode("utf-8", errors="replace") if isinstance(raw_html, bytes) else str(raw_html)
        else:
            body_html = "<pre>" + html.escape(str(getattr(msg, "body", "") or "")) + "</pre>"
    finally:
        try:
            msg.close()
        except Exception:
            pass

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_wrap_email_html(subject, sender, to, date, body_html))


# ---------------------------------------------------------------------------
# Office binario / RTF / ODF → docx|pptx|xlsx via LibreOffice headless
# ---------------------------------------------------------------------------

def _soffice_convert(src: str, target_ext: str, out_dir: str) -> str:
    """Converte `src` nel formato `target_ext` dentro `out_dir`, ritorna il path.

    Usa un UserInstallation isolato per invocazione: senza, due `soffice`
    concorrenti (il worker può avere più job in parallelo) si attaccano allo
    stesso profilo e la seconda non converte. Così ognuna è indipendente.
    """
    profile_dir = os.path.join(out_dir, ".loprofile")
    cmd = [
        "soffice", "--headless", "--norestore", "--nolockcheck",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to", target_ext, "--outdir", out_dir, src,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SOFFICE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"LibreOffice timeout (>{SOFFICE_TIMEOUT_SECONDS}s) su {os.path.basename(src)}")

    if proc.returncode != 0:
        raise RuntimeError(
            f"LibreOffice exit {proc.returncode} su {os.path.basename(src)}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )

    # soffice nomina l'output <basename>.<target_ext>; lo localizziamo nel dir
    # (che contiene solo la nostra conversione + il profilo isolato).
    produced = [
        os.path.join(out_dir, n) for n in os.listdir(out_dir)
        if n.lower().endswith("." + target_ext)
    ]
    if not produced:
        raise RuntimeError(f"LibreOffice non ha prodotto un .{target_ext} per {os.path.basename(src)}")
    return produced[0]


# ---------------------------------------------------------------------------
# Audio / video → VTT via faster-whisper (vedi utils/transcribe)
# ---------------------------------------------------------------------------

def _transcribe_media(src: str, out_vtt: str, is_video: bool) -> None:
    """Trascrive audio/video in VTT applicando il limite di durata (sweet spot).

    Controlla la durata con ffprobe PRIMA di trascrivere: oltre il limite il job
    fallisce con un messaggio che indica l'env da alzare — niente CPU sprecata.
    """
    from utils.transcribe import get_duration_seconds, transcribe_to_vtt
    from utils.settings import ASR_MAX_AUDIO_MINUTES, ASR_MAX_VIDEO_MINUTES

    limit_min = ASR_MAX_VIDEO_MINUTES if is_video else ASR_MAX_AUDIO_MINUTES
    dur = get_duration_seconds(src)
    if dur and dur > limit_min * 60:
        kind = "VIDEO" if is_video else "AUDIO"
        raise RuntimeError(
            f"durata {dur / 60:.0f} min oltre il limite {limit_min} min — "
            f"alza SOPHIA_VECTOR_ASR_MAX_{kind}_MINUTES per file più lunghi"
        )
    transcribe_to_vtt(src, out_vtt, is_video=is_video)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def normalize_for_parser(path: str) -> tuple[str, str | None]:
    """Restituisce un path che Docling sa parsare.

    (path_originale, None)  se è già un formato nativo.
    (path_convertito, tmp_dir)  se ha richiesto conversione — il chiamante deve
    rimuovere `tmp_dir` a fine parsing.
    Solleva `UnsupportedFormatError` se non è né nativo né convertibile.
    """
    # import locale per evitare cicli (docling importa da settings, non da qui)
    from utils.docling import (
        PARSER_NATIVE_EXTENSIONS,
        PARSER_CONVERTIBLE_EXTENSIONS,
        ASR_AUDIO_EXTENSIONS,
        ASR_VIDEO_EXTENSIONS,
    )

    ext = os.path.splitext(path)[1].lower()

    if ext in PARSER_NATIVE_EXTENSIONS:
        return path, None

    if ext not in PARSER_CONVERTIBLE_EXTENSIONS:
        raise UnsupportedFormatError(ext or "(nessuna estensione)")

    base = os.path.splitext(os.path.basename(path))[0]
    tmp_dir = _new_tmp_dir()
    try:
        if ext == ".eml":
            out = os.path.join(tmp_dir, base + ".html")
            _eml_to_html(path, out)
        elif ext == ".msg":
            out = os.path.join(tmp_dir, base + ".html")
            _msg_to_html(path, out)
        elif ext == ".txt":
            # Docling non ha un InputFormat 'txt': lo trattiamo come markdown
            # (testo semplice è markdown valido).
            out = os.path.join(tmp_dir, base + ".md")
            shutil.copyfile(path, out)
        elif ext in ASR_AUDIO_EXTENSIONS or ext in ASR_VIDEO_EXTENSIONS:
            # Trascrizione locale (faster-whisper) → VTT, che Docling chunka.
            out = os.path.join(tmp_dir, base + ".vtt")
            _transcribe_media(path, out, is_video=ext in ASR_VIDEO_EXTENSIONS)
        else:
            target = _OFFICE_TARGET[ext]
            out = _soffice_convert(path, target, tmp_dir)

        # check "non è rotto": output presente e non vuoto
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError(f"conversione {ext} ha prodotto un file vuoto")

        logger.info(f"🔁 {os.path.basename(path)} → {os.path.basename(out)} (pre-parser)")
        return out, tmp_dir

    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
