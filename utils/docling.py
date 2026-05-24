# filepath: utils/docling.py
"""
Docling API Client.

Gestisce la comunicazione con il servizio Docling per parsing e chunking documenti.
"""

import os
import logging
import traceback
from typing import Any

import requests
import time
# Config centralizzata (pydantic, prefisso SOPHIA_VECTOR_). I PARSER_* erano
# env legacy lette via os.getenv: ora vengono da utils/config tramite il shim.
from .settings import (
    DOCLING_URL,
    PARSER_MODEL_TOKENIZER,
    PARSER_MODEL_MAX_TOKENS,
    PARSER_USE_OCR,
    PARSER_PICTURE_DESCRIPTION,
    PARSER_TABLE_MODE,
    PARSER_TABLE_CELL_MATCHING,
    PARSER_PDF_BACKEND,
    PARSER_MAX_WAIT_SECONDS,
)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 3.0
# Connect timeout breve (il parser è in LAN/stesso host); il read timeout segue
# la config PARSER_MAX_WAIT_SECONDS, non più un 600 hardcoded scollegato dal
# convert_document_timeout passato nel body.
CONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, float(PARSER_MAX_WAIT_SECONDS))

logger = logging.getLogger("docling")
logger.setLevel(logging.DEBUG)

# Estensioni accettate per l'ingest (deve restare allineato a convert_from_formats
# in _build_chunking_params / _build_convert_params).
PARSER_SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".html", ".htm", ".md", ".xlsx",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif",
}

# Validazione PARSER_TABLE_MODE
if PARSER_TABLE_MODE not in ("fast", "accurate"):
    logger.warning(f"Invalid PARSER_TABLE_MODE '{PARSER_TABLE_MODE}', defaulting to 'accurate'")
    PARSER_TABLE_MODE = "accurate"

# Validazione PARSER_PDF_BACKEND
if PARSER_PDF_BACKEND not in ("pypdfium2", "dlparse_v1", "dlparse_v2", "dlparse_v4", "docling_parse"):
    logger.warning(f"Invalid PARSER_PDF_BACKEND '{PARSER_PDF_BACKEND}', defaulting to 'docling_parse'")
    PARSER_PDF_BACKEND = "docling_parse"


def _log_request_error(err: Exception, url: str) -> None:
    """Logga in modo uniforme gli errori delle request."""
    logger.error(f"[DOCLING REQUEST ERROR] URL: {url}")
    logger.error(f"Tipo errore: {type(err).__name__}")
    logger.error(f"Messaggio: {str(err)}")
    logger.error("Traceback completo:\n" + traceback.format_exc())


def _build_chunking_params() -> dict[str, Any]:
    """
    Costruisce i parametri per l'endpoint /chunk/hybrid/file.

    Riferimento: API spec con prefisso "convert_" e "chunking_"
    """
    return {
        # Output configuration
        "include_converted_doc": False,
        "target_type": "inbody",

        # Convert configuration (prefisso "convert_")
        "convert_from_formats": ["docx", "pptx", "html", "image", "pdf", "md", "xlsx"],
        "convert_image_export_mode": "placeholder",
        "convert_do_ocr": PARSER_USE_OCR,
        "convert_force_ocr": False,
        "convert_ocr_engine": "easyocr",
        "convert_ocr_lang": ["it", "en"],
        "convert_pdf_backend": PARSER_PDF_BACKEND,
        "convert_table_mode": PARSER_TABLE_MODE,
        "convert_table_cell_matching": PARSER_TABLE_CELL_MATCHING,
        "convert_pipeline": "standard",
        "convert_page_range": [1, 10000],
        "convert_document_timeout": PARSER_MAX_WAIT_SECONDS,
        "convert_abort_on_error": False,
        "convert_do_table_structure": True,
        "convert_include_images": False,
        "convert_images_scale": 0.1,
        "convert_md_page_break_placeholder": "<!-- page-break -->",
        "convert_do_code_enrichment": False,
        "convert_do_formula_enrichment": False,
        "convert_do_picture_classification": False,
        "convert_do_picture_description": PARSER_PICTURE_DESCRIPTION,
        "convert_picture_description_area_threshold": 0.05,

        # Chunking configuration (prefisso "chunking_")
        # Tabelle serializzate come triplets (non markdown): meno token, relazioni
        # chiave-valore esplicite e robustezza allo split (ogni riga è auto-contenuta).
        "chunking_use_markdown_tables": False,
        "chunking_include_raw_text": False,
        "chunking_max_tokens": PARSER_MODEL_MAX_TOKENS,
        "chunking_tokenizer": PARSER_MODEL_TOKENIZER,
        # merge_peers OFF: su documenti con tabelle dense (es. norme assuntive)
        # il merge dei chunk adiacenti tokenizza/confronta i peer ed esplode in
        # tempo (la conversione PDF è veloce, è il chunking che si pianta → era
        # la causa dei ReadTimeout in prod). Chunk un filo più frammentati ma
        # ingestion sbloccata; le tabelle restano in triplets (qualità invariata).
        "chunking_merge_peers": False,
    }


def _build_convert_params() -> dict[str, Any]:
    """
    Costruisce i parametri per l'endpoint /convert/file/async.

    Riferimento: API spec SENZA prefisso (nomi diretti)
    """
    return {
        # Output configuration
        "target_type": "inbody",
        "from_formats": ["docx", "pptx", "html", "image", "pdf", "md", "xlsx"],
        "to_formats": ["md"],

        # Image configuration
        "image_export_mode": "placeholder",
        "include_images": False,
        "images_scale": 0.1,

        # OCR configuration
        "do_ocr": PARSER_USE_OCR,
        "force_ocr": False,
        "ocr_engine": "easyocr",
        "ocr_lang": ["it", "en"],

        # PDF configuration
        "pdf_backend": PARSER_PDF_BACKEND,

        # Table configuration
        "table_mode": PARSER_TABLE_MODE,
        "table_cell_matching": PARSER_TABLE_CELL_MATCHING,
        "do_table_structure": True,

        # Processing configuration
        "pipeline": "standard",
        "page_range": [1, 10000],
        "document_timeout": PARSER_MAX_WAIT_SECONDS,
        "abort_on_error": False,

        # Markdown configuration
        "md_page_break_placeholder": "<!-- page-break -->",

        # Enrichment configuration
        "do_code_enrichment": False,
        "do_formula_enrichment": False,
        "do_picture_classification": False,
        "do_picture_description": PARSER_PICTURE_DESCRIPTION,
        "picture_description_area_threshold": 0.05,
    }


def get_task_status(task_id: str) -> dict:
    """Ottiene lo stato di un task asincrono."""
    url = f"{DOCLING_URL}/v1/status/poll/{task_id}"
    logger.debug(f"[GET] {url}")

    try:
        resp = requests.get(url, timeout=30)
        logger.debug(f"[RESPONSE STATUS] {resp.status_code}")
        logger.debug(f"[RESPONSE BODY] {resp.text[:800]}")
        resp.raise_for_status()
        return resp.json()

    except Exception as e:
        _log_request_error(e, url)
        raise


def get_task_results(task_id: str) -> dict:
    """Ottiene i risultati di un task completato."""
    url = f"{DOCLING_URL}/v1/result/{task_id}"
    logger.debug(f"[GET] {url}")

    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        logger.debug(f"[RESPONSE STATUS] {resp.status_code}")
        logger.debug(f"[RESPONSE BODY] {resp.text[:800]}")
        resp.raise_for_status()
        return resp.json()

    except Exception as e:
        _log_request_error(e, url)
        raise


def upload_file_for_chunking_task_async(file_path: str) -> dict:
    """
    Carica un file per chunking asincrono.

    Endpoint: POST /chunk/hybrid/file/async

    Returns:
        dict con task_id e task_status
    """
    url = f"{DOCLING_URL}/v1/chunk/hybrid/file/async"
    logger.debug(f"[POST FILE ASYNC] {url} – file: {file_path}")

    try:
        with open(file_path, "rb") as f:
            filename = os.path.basename(file_path)
            files = {"files": (filename, f, "application/octet-stream")}
            data = _build_chunking_params()

            resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT)
            logger.debug(f"[RESPONSE STATUS] {resp.status_code}")
            logger.debug(f"[RESPONSE BODY] {resp.text[:800]}")
            resp.raise_for_status()
            return resp.json()

    except Exception as e:
        _log_request_error(e, url)
        raise


def upload_file_for_parsing_task_async(file_path: str) -> dict:
    """
    Carica un file per parsing asincrono (senza chunking).

    Endpoint: POST /convert/file/async

    Returns:
        dict con task_id e task_status
    """
    url = f"{DOCLING_URL}/v1/convert/file/async"
    logger.debug(f"[POST FILE ASYNC] {url} – file: {file_path}")

    try:
        with open(file_path, "rb") as f:
            filename = os.path.basename(file_path)
            files = {"files": (filename, f, "application/octet-stream")}
            data = _build_convert_params()

            resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT)
            logger.debug(f"[RESPONSE STATUS] {resp.status_code}")
            logger.debug(f"[RESPONSE BODY] {resp.text[:800]}")
            resp.raise_for_status()
            return resp.json()

    except Exception as e:
        _log_request_error(e, url)
        raise


def upload_file_for_parsing_task_sync(file_path: str) -> dict:
    """
    Carica un file per parsing sincrono (senza chunking).

    Endpoint: POST /convert/file

    Returns:
        dict con task_id e task_status
    """
    url = f"{DOCLING_URL}/v1/convert/file"
    logger.debug(f"[POST FILE ASYNC] {url} – file: {file_path}")

    try:
        with open(file_path, "rb") as f:
            filename = os.path.basename(file_path)
            files = {"files": (filename, f, "application/octet-stream")}
            data = _build_convert_params()

            resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT)
            logger.debug(f"[RESPONSE STATUS] {resp.status_code}")
            logger.debug(f"[RESPONSE BODY] {resp.text[:800]}")
            resp.raise_for_status()
            return resp.json()

    except Exception as e:
        _log_request_error(e, url)
        raise


def upload_file_for_chunking_sync(file_path: str) -> dict:
    """
    Carica un file per chunking sincrono (con retry).
    """
    url = f"{DOCLING_URL}/v1/chunk/hybrid/file"
    logger.debug(f"[POST FILE SYNC] {url} – file: {file_path}")

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(file_path, "rb") as f:
                filename = os.path.basename(file_path)
                files = {"files": (filename, f, "application/octet-stream")}
                data = _build_chunking_params()

                resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT)
                logger.debug(f"[RESPONSE STATUS] {resp.status_code}")
                logger.debug(f"[RESPONSE BODY] {resp.text[:800]}")
                resp.raise_for_status()
                return resp.json()

        except requests.exceptions.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response else 0

            # Retry su 404 (bug Docling) o 5xx (server error)
            if status == 404 or status >= 500:
                delay = RETRY_DELAY_SECONDS * attempt
                logger.warning(
                    f"[DOCLING RETRY] Attempt {attempt}/{MAX_RETRIES} failed "
                    f"with status {status}. Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue

            # Altri errori (400, 422, etc.) non ritentare
            _log_request_error(e, url)
            raise

        except requests.exceptions.ReadTimeout as e:
            # Read timeout = il parse/chunk è genuinamente lungo o appeso.
            # Ritentare l'identica richiesta NON aiuta: rifà lo stesso lavoro
            # lungo e lascia task Docling duplicati (il parser ha 1 worker).
            # Falliamo subito così il worker single-concurrency non resta
            # bloccato MAX_RETRIES × timeout (era ~30 min a file in prod).
            _log_request_error(e, url)
            raise

        except requests.exceptions.RequestException as e:
            last_error = e
            delay = RETRY_DELAY_SECONDS * attempt
            logger.warning(
                f"[DOCLING RETRY] Attempt {attempt}/{MAX_RETRIES} failed: {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
            continue

    # Tutti i tentativi falliti
    if last_error:
        _log_request_error(last_error, url)
        raise last_error

    # Fallback (non dovrebbe mai accadere)
    raise RuntimeError(f"All {MAX_RETRIES} attempts failed for {url}")
