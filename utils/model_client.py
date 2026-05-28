"""
Client HTTP del worker verso gli endpoint NLP del backend (/v1/nlp/*).

I modelli vivono nel processo backend ("owner"); il worker — che è già un consumer HTTP
di Docling, BGE-M3 e Qdrant — chiede qui l'estrazione invece di caricarli in-process.
Una sola copia dei modelli, niente doppione.

Best-effort: un guasto (backend non pronto, timeout, 5xx) NON fa fallire l'ingestion →
ritorna liste vuote allineate ai testi, esattamente come degradava l'estrazione quando il
modello non era disponibile. Stesso contratto di ritorno dei vecchi registry.*.extract().
"""

import os
import requests
from typing import Any, Dict, List, Optional

from utils.logger import get_logger
from utils.config import settings

logger = get_logger(__name__)

# Sessione riusata (keep-alive verso il backend localhost). Read timeout generoso: la
# prima richiesta paga il lazy-load del modello nel backend.
_session = requests.Session()
_TIMEOUT = (10, 180)  # (connect, read)


def _headers() -> Dict[str, str]:
    """Inoltra l'API key se il backend la richiede (stesso valore del proxy frontend)."""
    return {"authorization": f"Bearer {settings.API_KEY}"} if settings.API_KEY else {}


def _post(path: str, payload: Dict[str, Any], n: int) -> List[List[Dict[str, Any]]]:
    """POST best-effort a {INTERNAL_API_URL}/v1/nlp{path}. Ritorna `results` (lista
    allineata a `texts`) oppure liste vuote se qualcosa va storto."""
    empty: List[List[Dict[str, Any]]] = [[] for _ in range(n)]
    try:
        r = _session.post(
            f"{settings.INTERNAL_API_URL}/v1/nlp{path}",
            json=payload, headers=_headers(), timeout=_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("results")
        if not isinstance(results, list) or len(results) != n:
            logger.warning(f"model_client{path}: risposta inattesa, estrazione degradata a vuoto")
            return empty
        return results
    except Exception as e:
        logger.warning(f"model_client{path} fallito ({n} testi), estrazione degradata: {e}")
        return empty


def ner(texts: List[str], labels: Optional[List[str]] = None) -> List[List[Dict[str, Any]]]:
    """Entità per ogni testo (GLiNER nel backend). Allineato a `texts`."""
    if not texts:
        return []
    return _post("/ner", {"texts": texts, "labels": labels}, len(texts))


def classify(
    texts: List[str], labels: Optional[List[str]] = None, threshold: Optional[float] = None
) -> List[List[Dict[str, Any]]]:
    """Classi per ogni testo (GliClass nel backend). Allineato a `texts`."""
    if not texts:
        return []
    return _post("/classify", {"texts": texts, "labels": labels, "threshold": threshold}, len(texts))


def relex(
    texts: List[str],
    entity_labels: Optional[List[str]] = None,
    relation_labels: Optional[List[str]] = None,
) -> List[List[Dict[str, Any]]]:
    """Relazioni tipizzate per ogni testo (GLiNER-relex nel backend). Allineato a `texts`."""
    if not texts:
        return []
    return _post(
        "/relex",
        {"texts": texts, "entity_labels": entity_labels, "relation_labels": relation_labels},
        len(texts),
    )


def transcribe(file_path: str) -> str:
    """Trascrive un file audio/video via il backend (Whisper) e ritorna il VTT.

    A differenza di ner/classify/relex (best-effort → vuoto), questa SOLLEVA su errore:
    la trascrizione È il contenuto del documento, quindi un fallimento deve far fallire
    il job (niente COMPLETED muto). Read timeout lungo: i video richiedono tempo."""
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
        r = _session.post(
            f"{settings.INTERNAL_API_URL}/v1/nlp/transcribe",
            files=files, headers=_headers(), timeout=(10, 3600),
        )
    r.raise_for_status()
    return r.json().get("vtt", "")
