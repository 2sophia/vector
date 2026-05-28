import requests
import traceback
from typing import List, Tuple, Dict, Any
from qdrant_client.models import SparseVector

from .settings import BGE_M3_URL
from utils.logger import get_logger

# Logger standard del progetto: rispetta il livello globale (INFO in prod) invece
# di forzare DEBUG con un handler manuale, che stampava in chiaro response body e
# query/testi dei documenti anche in produzione.
logger = get_logger(__name__)

# Sessione HTTP riusata: keep-alive verso il servizio BGE-M3 invece di aprire una
# nuova connessione TCP a ogni batch (sotto ingestion sono migliaia → overhead e
# rischio di esaurire le porte effimere).
_session = requests.Session()


def _log_request_error(err: Exception, url: str, extra: Dict[str, Any] | None = None):
    """
    Logga in modo uniforme gli errori delle request verso BGE-M3.
    """
    logger.error(f"[BGE REQUEST ERROR] URL: {url}")
    if extra:
        logger.error(f"[BGE REQUEST CONTEXT] {extra}")
    logger.error(f"Tipo errore: {type(err).__name__}")
    logger.error(f"Messaggio: {str(err)}")
    logger.error("Traceback completo:\n" + traceback.format_exc())


def get_bge_embeddings(
        texts: List[str],
        return_dense: bool = True,
        return_sparse: bool = True,
        return_colbert: bool = False,
) -> Tuple[List[List[float]] | None, List[SparseVector] | None]:
    """
    Chiama il servizio BGE-M3 e restituisce (dense_list, sparse_list).
    Se len(texts) == 1, ritorna direttamente il singolo vettore.
    """
    if not texts:
        logger.debug("[BGE EMBEDDINGS] Nessun testo fornito, ritorno liste vuote.")
        return [], []

    url = f"{BGE_M3_URL}/v1/embeddings"
    payload = {
        "input": texts,
        "return_dense": return_dense,
        "return_sparse": return_sparse,
        "return_colbert": return_colbert,
    }

    # Log della richiesta (senza esplodere il log con tutti i testi)
    logger.debug(
        f"[BGE EMBEDDINGS][POST] {url} | num_texts={len(texts)} "
        f"| return_dense={return_dense} | return_sparse={return_sparse} | return_colbert={return_colbert}"
    )

    try:
        resp = _session.post(url, json=payload, timeout=120)
        logger.debug(f"[BGE EMBEDDINGS][RESPONSE STATUS] {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log_request_error(
            e,
            url,
            extra={"num_texts": len(texts), "return_dense": return_dense, "return_sparse": return_sparse},
        )
        raise

    dense_list: List[List[float]] | None = None
    sparse_list: List[SparseVector] | None = None

    results = data.get("results", [])
    logger.debug(f"[BGE EMBEDDINGS] results_count={len(results)}")

    if return_dense:
        dense_list = [r["embeddings"]["dense"] for r in results]

    if return_sparse:
        sparse_list = []
        for r in results:
            sparse = r["embeddings"]["sparse"]
            sv = SparseVector(
                indices=sparse["indices"],
                values=sparse["values"],
            )
            sparse_list.append(sv)

    logger.debug(
        "[BGE EMBEDDINGS] Batch texts → returning lists "
        f"(dense_len={len(dense_list) if dense_list is not None else 0}, "
        f"sparse_len={len(sparse_list) if sparse_list is not None else 0})"
    )
    return dense_list, sparse_list


def get_bge_reranking_docs(query: str, documents: List[str], weights=None) -> List[Dict[str, Any]]:
    if not documents:
        logger.debug("[BGE RERANK] Nessun documento fornito, ritorno [].")
        return []

    url = f"{BGE_M3_URL}/v1/rerank"
    payload = {
        "query": query,
        "documents": documents,
    }

    if weights is not None:
        payload["weights"] = weights

    logger.debug(
        f"[BGE RERANK][POST] {url} | num_documents={len(documents)} | query_len={len(query)}"
    )

    try:
        resp = _session.post(url, json=payload, timeout=120)
        logger.debug(f"[BGE RERANK][RESPONSE STATUS] {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log_request_error(
            e, url, extra={"num_documents": len(documents)}
        )
        raise

    results = data.get("results", [])
    logger.debug(f"[BGE RERANK] results_count={len(results)}")
    return results
