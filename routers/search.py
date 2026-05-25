"""Search endpoints"""

import os
import re

import traceback

from fastapi import APIRouter, HTTPException

# Import DA UTILS (non serve il punto perché siamo fuori dal pacchetto)
from utils import (
    get_logger,
    qdrant_client,
)

from utils.qdrant import qdrant_hybrid_batch_search, qdrant_lexical_search
from utils.embeddings import get_bge_reranking_docs
from utils.falkor import expand_neighbors
from utils.fusion import rrf_fuse

from utils.schemas import VectorSearch, SearchResponse

from utils.settings import (
    FILES_STORAGE,
    CURATION_ENABLED,
    CURATION_BOILERPLATE_RATIO,
    CURATION_BOILERPLATE_MIN_DOCS,
)
from utils.curation import body_hash, boilerplate_hashes

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


# token "esatti" dove il dense embedding è debole: frasi tra virgolette, riferimenti
# numerici (231/2001, 2026-001), codici alfanumerici (D.lgs, IT60X...), acronimi.
_QUOTED_RE = re.compile(r'"([^"]{2,})"')
_NUMREF_RE = re.compile(r'\b\d+(?:[/.\-]\d+)+\b')
_ALNUM_RE = re.compile(r'\b(?=\w*\d)(?=\w*[A-Za-z])[\w.]{2,}\b')
_ACRONYM_RE = re.compile(r'\b[A-Z]{3,}\b')


def _extract_lexical_terms(query_text):
    """Estrae dalla query i termini ESATTI su cui il dense è debole (codici,
    riferimenti, acronimi, frasi tra virgolette). Vuoto se non ce ne sono → il canale
    lessicale viene saltato (zero costo sulle query in linguaggio naturale)."""
    terms = set()
    for m in _QUOTED_RE.findall(query_text):
        terms.add(m.strip())
    for rx in (_NUMREF_RE, _ALNUM_RE, _ACRONYM_RE):
        terms.update(rx.findall(query_text))
    return [t for t in terms if len(t) >= 3]


def _augment(vector_store_id, query_text, direct, search_data):
    """Retrieval multi-canale fuso con RRF + un solo rerank finale.

    Canali: **vettoriale** (Qdrant, sempre — dense+sparse già fusi a monte), **grafo**
    (se `graph_expand`: entità condivise + :NEXT), **lessicale/exact-match** (se la
    query ha codici/riferimenti: recupera chunk che li contengono, dove il dense
    fallisce). RRF fonde i ranghi dei canali → pool di candidati; il cross-encoder fa
    il ranking finale. Se non c'è alcun canale extra ritorna i diretti invariati.
    """
    cand_by_id = {d["id"]: d for d in direct if d.get("id")}
    channels = [[d["id"] for d in direct if d.get("id")]]

    # --- canale grafo (opzionale) ---
    if getattr(search_data, "graph_expand", False) and direct:
        seeds = [d["id"] for d in direct if d.get("id")][:10]
        slugs = sorted({
            (d.get("payload") or {}).get("sophia_directory_slug")
            for d in direct if (d.get("payload") or {}).get("sophia_directory_slug")
        })
        neighbors = expand_neighbors(
            vector_store_id, seeds, slugs=slugs or None,
            limit=search_data.graph_neighbors or 20,
            df_max=search_data.graph_df_max or 0.5,
        )
        glist = []
        for nb in neighbors:
            pid = nb.get("qdrant_point_id")
            if not pid:
                continue
            glist.append(pid)
            cand_by_id.setdefault(pid, {
                "id": pid, "score": None, "score_qdrant": None,
                "filename": nb.get("filename"), "file_id": nb.get("file_id"),
                "payload": {
                    "text": nb.get("text"), "filename": nb.get("filename"),
                    "file_id": nb.get("file_id"), "sophia_directory_slug": nb.get("slug"),
                },
                "source": nb.get("source"), "via": nb.get("via"),
            })
        if glist:
            channels.append(glist)

    # --- canale lessicale / exact-match (opzionale, se ci sono termini esatti) ---
    terms = _extract_lexical_terms(query_text)
    if terms:
        try:
            lex = qdrant_lexical_search(vector_store_id, terms, search_data, limit=30)
        except Exception as e:
            logger.warning(f"[lexical] fetch fallito: {e}")
            lex = []
        llist = []
        for pt in lex:
            pid = str(pt.id)
            llist.append(pid)
            payload = pt.payload or {}
            cand_by_id.setdefault(pid, {
                "id": pid, "score": None, "score_qdrant": None,
                "filename": payload.get("filename"), "file_id": payload.get("file_id"),
                "payload": payload, "source": "lexical",
            })
        if llist:
            channels.append(llist)
            logger.info(f"[lexical] termini={terms} → {len(llist)} chunk exact-match")

    # nessun canale extra oltre al vettoriale → niente da fondere
    if len(channels) <= 1:
        return direct

    # RRF: fonde i ranghi dei canali (accordo tra canali premiato)
    fused_score = dict(rrf_fuse(channels))

    # pool candidati in ordine RRF, dedup per contenuto
    candidates, seen_text = [], set()
    for cid, _s in sorted(fused_score.items(), key=lambda kv: kv[1], reverse=True):
        c = cand_by_id.get(cid)
        if not c:
            continue
        key = ((c.get("payload") or {}).get("text") or "")[:200]
        if key and key in seen_text:
            continue
        seen_text.add(key)
        candidates.append(c)

    top_n = search_data.max_num_results or 15

    # rerank unico sul pool fuso (cross-encoder query↔doc); fallback su ordine RRF
    docs = [(c.get("payload") or {}).get("text") or "" for c in candidates]
    try:
        reranked = get_bge_reranking_docs(query_text, docs)
    except Exception as e:
        logger.warning(f"[augment] rerank fallito, fallback RRF: {e}")
        rows = []
        for c in candidates[:top_n]:
            c["score"] = fused_score.get(c["id"])
            rows.append(c)
        return rows

    order = sorted(reranked, key=lambda r: r.get("relevance_score", 0.0), reverse=True)
    rows = []
    for r in order[:top_n]:
        c = candidates[r["index"]]
        c["score"] = r.get("relevance_score")
        rows.append(c)
    logger.info(f"[augment] {len(channels)} canali → {len(candidates)} candidati → top {len(rows)}")
    return rows


def _suppress_boilerplate(vector_store_id, rows):
    """Data curation a search-time: toglie dai risultati i chunk il cui BODY
    (testo senza il prefisso heading) compare in oltre RATIO dei documenti della
    collection (e in almeno MIN_DOCS) — disclaimer, intestazioni e firme ripetute
    non devono saturare i top-K passati all'LLM. È la tesi "meno dati puliti =
    output migliore" applicata dove conta. No-op se il layer è disattivo. Failsafe:
    se togliere svuoterebbe del tutto i risultati, li lascia (meglio rumore che vuoto).
    """
    if not CURATION_ENABLED or not rows:
        return rows
    row_hashes = [
        body_hash((r.get("payload") or {}).get("text") or "",
                  (r.get("payload") or {}).get("headings"))
        for r in rows
    ]
    bp = boilerplate_hashes(
        vector_store_id, row_hashes,
        CURATION_BOILERPLATE_RATIO, CURATION_BOILERPLATE_MIN_DOCS,
    )
    if not bp:
        return rows
    kept = [r for r, h in zip(rows, row_hashes) if h not in bp]
    removed = len(rows) - len(kept)
    if removed:
        logger.info(f"[curation] soppressi {removed} chunk boilerplate ({vector_store_id})")
    return kept or rows


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

        # ========== Retrieval multi-canale fuso con RRF ==========
        # _augment decide quali canali aggiungere: grafo (se graph_expand) e
        # lessicale/exact-match (se la query ha codici/riferimenti). No-op se non c'è
        # alcun canale extra → query in linguaggio naturale senza grafo restano com'erano.
        result_rows = _augment(vector_store_id, query_text, result_rows, search_data)

        # ========== Data curation: sopprimi il boilerplate dai risultati ==========
        result_rows = _suppress_boilerplate(vector_store_id, result_rows)

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
