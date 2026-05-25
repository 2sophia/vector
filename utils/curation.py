"""
Data curation — dedup del CONTENUTO dei chunk (non dei dati).

Tesi (vedi `data-curation-dedup-sota.md`): ingerire MEGLIO, non di più. Il valore
non è ridurre i punti Qdrant — il retrieval già deduplica per testo a query-time
(`routers/search.py`) — ma usare la *molteplicità* del contenuto come segnale: un
blocco di testo che compare identico in centinaia di documenti è boilerplate
(intestazioni, disclaimer, firme) e va tolto dai risultati, non passato all'LLM.

Punto chiave verificato sul parser: Docling contestualizza il testo del chunk con
un prefisso heading "inbody" (`titolo-doc \n sezione \n body`). Lo stesso disclaimer
sotto sezioni o documenti diversi ha quindi un `text` DIVERSO. Per riconoscerlo come
duplicato si lavora sul BODY (testo senza il prefisso heading), ricostruito da
`headings` — vedi `strip_headings`.

Store (MongoDB, per-collection, niente vettori — solo hash + provenienza):

    curation_bodies {
        _id: "<vector_store_id>:<body_hash>",
        vector_store_id, body_hash,
        files: [file_id, ...]          # doc_count = len(files)
    }

Il conteggio su Mongo è la fonte di verità per la soppressione boilerplate a
search-time (non dipende dal grafo). Le funzioni sono sincrone: chiamarle via
`asyncio.to_thread` dal worker / dagli endpoint async. Tutto best-effort: un errore
qui viene loggato ma non rompe ingestion né ricerca.
"""

import re
import hashlib
from typing import Dict, List, Optional, Sequence

from pymongo import UpdateOne

from utils.database import db
from utils.logger import get_logger

logger = get_logger(__name__)

_bodies = db["curation_bodies"]
_jobs = db["ingestion_jobs"]

_WS_RE = re.compile(r"\s+")

# Indici best-effort: i path di purge/stats/delete filtrano per vector_store_id,
# il purge per-file matcha anche dentro l'array `files` (multikey). create_index è
# idempotente → no-op se già presente. Un guasto qui non deve impedire l'import.
try:
    _bodies.create_index("vector_store_id")
    _bodies.create_index([("vector_store_id", 1), ("files", 1)])
except Exception as _e:  # pragma: no cover
    logger.warning(f"curation_bodies index setup failed: {_e}")


# ---------------------------------------------------------------------------
# Hashing del body (heading-stripped)
# ---------------------------------------------------------------------------

def strip_headings(text: str, headings: Optional[Sequence[str]]) -> str:
    """Rimuove il prefisso heading "inbody" che Docling antepone al body del chunk.

    Docling serializza il chunk come `"\\n".join(headings) + "\\n" + body`. Tolgo
    quel prefisso così il body è confrontabile tra documenti/sezioni diversi (lo
    stesso disclaimer sotto `Sezione A` o `Sezione B`, o in due circolari con titolo
    diverso, collassa sullo stesso body). Robusto: se il prefisso non combacia come
    atteso (formati senza heading, edge case) ritorna il testo intero — al massimo
    il dedup è più conservativo, mai sbagliato.
    """
    if not text:
        return ""
    body = text
    for h in (headings or []):
        if not h:
            continue
        candidate = body.lstrip()
        if candidate.startswith(h):
            body = candidate[len(h):].lstrip("\n")
        else:
            # heading non in testa come atteso → non forzo lo strip
            break
    body = body.strip()
    return body or text.strip()


def _normalize(s: str) -> str:
    """Normalizza per il confronto: collassa whitespace + lowercase."""
    return _WS_RE.sub(" ", s).strip().lower()


def body_hash(text: str, headings: Optional[Sequence[str]] = None) -> str:
    """sha256 del body normalizzato (heading-stripped, ws-collapsed, lowercase)."""
    body = strip_headings(text, headings)
    return hashlib.sha256(_normalize(body).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Provenienza per-collection (Mongo) — registra/purga
# ---------------------------------------------------------------------------

def register_document_bodies(
    vector_store_id: str, file_id: str, body_hashes: Sequence[str]
) -> None:
    """Registra che `file_id` contiene i body con questi hash.

    Dedup intra-documento: un hash conta UNA volta per documento (un disclaimer
    ripetuto 10 volte nello stesso file = un solo doc nel conteggio). Idempotente
    grazie a `$addToSet`: il re-ingest dello stesso file non gonfia i conteggi.
    """
    uniq = {h for h in body_hashes if h}
    if not uniq:
        return
    ops = [
        UpdateOne(
            {"_id": f"{vector_store_id}:{h}"},
            {
                "$setOnInsert": {"vector_store_id": vector_store_id, "body_hash": h},
                "$addToSet": {"files": file_id},
            },
            upsert=True,
        )
        for h in uniq
    ]
    try:
        _bodies.bulk_write(ops, ordered=False)
    except Exception as e:
        logger.warning(f"register_document_bodies({vector_store_id}, {file_id}) failed: {e}")


def purge_file_bodies(vector_store_id: str, file_id: str) -> None:
    """Re-ingest/delete di un file: lo toglie dalle liste di provenienza e rimuove
    le entry rimaste senza alcun file. Idempotente. Mantiene i conteggi corretti
    sotto la filosofia re-ingest del repo (index-new-then-delete-old)."""
    try:
        _bodies.update_many(
            {"vector_store_id": vector_store_id, "files": file_id},
            {"$pull": {"files": file_id}},
        )
        _bodies.delete_many({"vector_store_id": vector_store_id, "files": {"$size": 0}})
    except Exception as e:
        logger.warning(f"purge_file_bodies({vector_store_id}, {file_id}) failed: {e}")


def delete_collection_bodies(vector_store_id: str) -> None:
    """Cancella tutte le entry di provenienza di una collection (delete dello store)."""
    try:
        _bodies.delete_many({"vector_store_id": vector_store_id})
    except Exception as e:
        logger.warning(f"delete_collection_bodies({vector_store_id}) failed: {e}")


# ---------------------------------------------------------------------------
# Boilerplate: lettura a search-time + metriche
# ---------------------------------------------------------------------------

def total_documents(vector_store_id: str) -> int:
    """Numero di documenti (file distinti COMPLETED) nella collection — denominatore
    del ratio di boilerplate."""
    try:
        return len(_jobs.distinct(
            "file_id", {"vector_store_id": vector_store_id, "status": "COMPLETED"}
        ))
    except Exception:
        return 0


def boilerplate_hashes(
    vector_store_id: str,
    candidate_hashes: Sequence[str],
    ratio: float,
    min_docs: int,
    total_docs: Optional[int] = None,
) -> Dict[str, int]:
    """Dato un set di body_hash (dai risultati di ricerca), ritorna `{hash: doc_count}`
    per quelli che sono boilerplate: presenti in almeno `min_docs` documenti E in
    oltre `ratio` della collection. Una sola query Mongo. Best-effort → {} se errore.
    """
    uniq = [h for h in set(candidate_hashes) if h]
    if not uniq:
        return {}
    if total_docs is None:
        total_docs = total_documents(vector_store_id)
    try:
        rows = _bodies.find(
            {"_id": {"$in": [f"{vector_store_id}:{h}" for h in uniq]}},
            {"body_hash": 1, "files": 1},
        )
        out: Dict[str, int] = {}
        for r in rows:
            dc = len(r.get("files") or [])
            if dc >= min_docs and (total_docs <= 0 or dc / total_docs >= ratio):
                out[r["body_hash"]] = dc
        return out
    except Exception as e:
        logger.warning(f"boilerplate_hashes({vector_store_id}) failed: {e}")
        return {}


def curation_stats(vector_store_id: str, ratio: float, min_docs: int) -> Dict[str, int]:
    """Metriche di curation per una collection — "quanto boilerplate hai".

    Ritorna: documenti totali, contenuti (body) distinti, quanti di questi sono
    boilerplate (in ≥ min_docs doc e oltre `ratio`), e in quanti documenti compare
    il boilerplate più diffuso. Numero che racconta la tesi, e diagnostica per
    tarare le soglie sui dati reali.
    """
    total_docs = total_documents(vector_store_id)
    stats = {
        "total_documents": total_docs,
        "distinct_contents": 0,
        "boilerplate_contents": 0,
        "max_doc_frequency": 0,
    }
    try:
        cursor = _bodies.find({"vector_store_id": vector_store_id}, {"files": 1})
        for r in cursor:
            dc = len(r.get("files") or [])
            stats["distinct_contents"] += 1
            stats["max_doc_frequency"] = max(stats["max_doc_frequency"], dc)
            if dc >= min_docs and (total_docs <= 0 or dc / total_docs >= ratio):
                stats["boilerplate_contents"] += 1
    except Exception as e:
        logger.warning(f"curation_stats({vector_store_id}) failed: {e}")
    return stats
