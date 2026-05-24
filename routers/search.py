"""Search endpoints"""

import os

import traceback

from fastapi import APIRouter, HTTPException

# Import DA UTILS (non serve il punto perché siamo fuori dal pacchetto)
from utils import (
    get_logger,
    qdrant_client,
)

from utils.qdrant import qdrant_hybrid_batch_search
from utils.embeddings import get_bge_reranking_docs
from utils.falkor import expand_neighbors

from utils.schemas import VectorSearch, SearchResponse

from utils.settings import FILES_STORAGE

logger = get_logger(__name__)

# Crea il router (come una mini-app)
router = APIRouter(
    prefix="/v1/vector_stores",  # Tutti gli endpoint iniziano con questo
    tags=["Search"]  # Per la documentazione
)


def extract_attr(obj, key, default=None):
    """
    Ritorna obj[key] se è un dict, altrimenti getattr(obj, key).
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_point(point):
    # ID
    point_id = extract_attr(point, "id")

    # Payload (sempre dict, ma fallback a {} se manca)
    payload = extract_attr(point, "payload", {}) or {}

    # Score di Qdrant (base, senza rerank)
    score_qdrant = extract_attr(point, "score_qdrant", None)
    if score_qdrant is None:
        # In molti casi, sugli ScoredPoint Qdrant il campo è "score"
        score_qdrant = extract_attr(point, "score", None)

    # Score del reranker (se esiste)
    score_rerank = extract_attr(point, "score_rerank", None)

    # Score finale:
    # - se c'è il rerank => usa quello
    # - altrimenti usa lo score qdrant/base
    final_score = score_rerank if score_rerank is not None else score_qdrant

    return {
        "id": str(point_id) if point_id is not None else None,
        "score": final_score,
        "score_qdrant": score_qdrant,
        "filename": payload.get("filename"),
        "file_id": payload.get("file_id"),
        "payload": payload,
    }


def _graph_augment(vector_store_id, query_text, direct, search_data):
    """M4 — graph-augmented retrieval.

    Dai chunk diretti di Qdrant espande il vicinato nel grafo (entità condivise +
    :NEXT, filtrato per slug), unisce, e ri-rerankizza TUTTO con BGE-M3 → top-N.
    Se non c'è nulla da espandere o qualcosa va storto, ritorna i diretti invariati.
    """
    seeds = [d["id"] for d in direct if d.get("id")][:10]
    if not seeds:
        return direct
    # slug presenti nei risultati → l'espansione resta nella stessa directory
    slugs = sorted({
        (d.get("payload") or {}).get("sophia_directory_slug")
        for d in direct
        if (d.get("payload") or {}).get("sophia_directory_slug")
    })

    neighbors = expand_neighbors(
        vector_store_id, seeds,
        slugs=slugs or None,
        limit=search_data.graph_neighbors or 20,
        df_max=search_data.graph_df_max or 0.5,
    )
    if not neighbors:
        return direct

    # candidati = diretti (Qdrant) + vicini (grafo), dedup per point_id
    candidates = list(direct)
    seen = {d["id"] for d in direct}
    for nb in neighbors:
        pid = nb.get("qdrant_point_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        candidates.append({
            "id": pid,
            "score": None,
            "score_qdrant": None,
            "filename": nb.get("filename"),
            "file_id": nb.get("file_id"),
            "payload": {
                "text": nb.get("text"),
                "filename": nb.get("filename"),
                "file_id": nb.get("file_id"),
                "sophia_directory_slug": nb.get("slug"),
            },
            "source": nb.get("source"),
            "via": nb.get("via"),
        })

    # dedup per contenuto: lo stesso chunk può arrivare da più directory/path
    deduped, seen_text = [], set()
    for c in candidates:
        key = ((c.get("payload") or {}).get("text") or "")[:200]
        if key and key in seen_text:
            continue
        seen_text.add(key)
        deduped.append(c)
    candidates = deduped

    # rerank unificato vs query. Il servizio BGE è un cross-encoder: scora
    # direttamente la coppia query↔documento, i vecchi `weights` dense/sparse/
    # colbert sono ignorati → non li passiamo.
    docs = [(c.get("payload") or {}).get("text") or "" for c in candidates]
    try:
        reranked = get_bge_reranking_docs(query_text, docs)
    except Exception as e:
        logger.warning(f"[M4] rerank set espanso fallito: {e}")
        return direct

    order = sorted(reranked, key=lambda r: r.get("relevance_score", 0.0), reverse=True)
    top_n = search_data.max_num_results or 15
    rows = []
    for r in order[:top_n]:
        c = candidates[r["index"]]
        c["score"] = r.get("relevance_score")
        rows.append(c)
    logger.info(f"[M4] {len(direct)} diretti + {len(neighbors)} vicini → top {len(rows)} dopo rerank")
    return rows


# ================== VECTOR SEARCH ENDPOINT ==================

@router.post("/{vector_store_id}/search", response_model=SearchResponse)
def search_vector_store(vector_store_id: str, search_data: VectorSearch):
    """
    Strategia ottimizzata per vLLM locale:
    1. Loop per generare embeddings (uno ad uno, no limits)
    2. Accumula tutti gli embeddings
    3. Batch query Qdrant con TUTTI gli embeddings insieme
    4. Prendi solo i top candidati
    """
    try:
        if not vector_store_id.startswith("vs_"):
            raise HTTPException(status_code=404, detail="Vector store not found")

        collections = [c.name for c in qdrant_client.get_collections().collections]
        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        # ========== STEP 1: PREPARA I CHUNK ==========
        query_text = search_data.query

        # if search_data.file_id:
        #     file_path = os.path.join(FILES_STORAGE, search_data.file_id)
        #     metadata_path = os.path.join(FILES_STORAGE, f"{search_data.file_id}_metadata.json")
        # 
        #     if not os.path.exists(file_path) or not os.path.exists(metadata_path):
        #         raise HTTPException(status_code=404, detail="File not found")
        # 
        #         # new extract + chunk
        #         # parsed_docs = parse_with_docling(file_path)
        # 
        #     raise HTTPException(status_code=422, detail="not implemented | Could not extract text from file")

        logger.info(f"Processing {query_text}...")

        # new version of search
        final_results = qdrant_hybrid_batch_search(
            collection_name=vector_store_id,
            query_text=query_text,
            search_data=search_data
        )

        # Normalizza i risultati diretti di Qdrant
        result_rows = [normalize_point(point) for point in final_results]
        for r in result_rows:
            r["source"] = "qdrant"

        # ========== M4: graph-augmented retrieval (opzionale, dietro flag) ==========
        if getattr(search_data, "graph_expand", False):
            result_rows = _graph_augment(vector_store_id, query_text, result_rows, search_data)

        # ========== STEP 9: BUILD RESPONSE ==========
        search_results = []
        for norm in result_rows:
            payload = norm.get("payload") or {}

            result_data = {
                "id": norm["id"],
                "score": norm["score"],  # score finale (rerank se c'è, altrimenti qdrant)
                "filename": norm.get("filename"),
                "file_id": norm.get("file_id"),
                "score_qdrant": norm.get("score_qdrant"),  # score "grezzo" di Qdrant
            }

            # Distance basata sul NUOVO score (reranker) se presente
            if search_data.include_distances and norm["score"] is not None:
                result_data["distance"] = 1 - norm["score"]

            # Metadata extra (tutto il payload tranne alcuni campi)
            if search_data.include_metadata:
                metadata = {
                    k: v
                    for k, v in payload.items()
                    if k not in ['text', 'filename', 'vector_store_id', 'file_id']
                }
                # provenienza M4: qdrant | graph:mentions | graph:next (+ entità ponte)
                if norm.get("source"):
                    metadata["_source"] = norm["source"]
                if norm.get("via"):
                    metadata["_via"] = norm["via"]
                result_data["attributes"] = metadata

            # Content testuale
            result_data["content"] = payload.get("text")

            search_results.append(result_data)

        # ============ Costruisci la risposta ============

        return {
            "object": "vector_store.search",
            "data": search_results,
            "query": query_text,
            "usage": {
                "total_tokens": len(query_text),
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search error: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")
