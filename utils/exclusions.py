"""
Esclusione manuale dei file dall'ingestion.

Un utente/dev che vede errori ripetuti su file che palesemente NON vanno ingestiti
(es. carte intestate vuote, artwork di stampa, immagini-logo) può marcarli
EXCLUDED: il sistema li salta ovunque — vector worker, sync SharePoint e ogni
futura source — e ripulisce i dati eventualmente già indicizzati (Qdrant + grafo +
curation). La sync continua a *vederli* ma li conta come esclusi: non li ri-scarica,
non ricrea job, e col cron li salta.

Fonte di verità UNICA: la collection `excluded_files`, chiavata su
(vector_store_id, file_id) per i file manuali e su (vector_store_id,
sharepoint_file_id) per i file SharePoint — perché il file_id SharePoint viene
RIGENERATO a ogni sync, mentre lo sharepoint_file_id (l'id Graph) è stabile.

Tutti i punti di enforcement passano da `is_excluded()`: una sola logica, non
sparsa in mille parti.
"""

import logging
from typing import Any, Dict, List, Optional

from utils.database import db
from utils import get_timestamp
from utils.qdrant import delete_qdrant_points
from utils.falkor import purge_file_graph
from utils.curation import purge_file_bodies

logger = logging.getLogger("exclusions")

excluded_coll = db["excluded_files"]
ingestion_jobs = db["ingestion_jobs"]


def is_excluded(
    vector_store_id: str,
    *,
    file_id: Optional[str] = None,
    sharepoint_file_id: Optional[str] = None,
) -> bool:
    """True se il file è marcato EXCLUDED nel vector store, per file_id OPPURE per
    sharepoint_file_id. Lookup leggero su due chiavi indicizzate."""
    if not vector_store_id:
        return False
    ors: List[Dict[str, Any]] = []
    if file_id:
        ors.append({"file_id": file_id})
    if sharepoint_file_id:
        ors.append({"sharepoint_file_id": sharepoint_file_id})
    if not ors:
        return False
    return excluded_coll.find_one(
        {"vector_store_id": vector_store_id, "$or": ors}, {"_id": 1}
    ) is not None


def list_excluded(vector_store_id: str) -> List[Dict[str, Any]]:
    """Elenco dei file esclusi del vector store (più recenti prima)."""
    out: List[Dict[str, Any]] = []
    for d in excluded_coll.find({"vector_store_id": vector_store_id}).sort("excluded_at", -1):
        d.pop("_id", None)
        out.append(d)
    return out


def _purge_file_data(vector_store_id: str, file_id: str) -> Dict[str, bool]:
    """Rimuove i dati già indicizzati per il file (idempotente, best-effort): punti
    Qdrant + nodi grafo + provenienza curation. Un guasto su un layer non blocca gli altri."""
    out = {"qdrant": False, "graph": False, "curation": False}
    try:
        delete_qdrant_points(collection_name=vector_store_id, field_name="file_id", field_value=file_id)
        out["qdrant"] = True
    except Exception as e:
        logger.warning(f"purge qdrant {file_id}: {e}")
    try:
        purge_file_graph(vector_store_id, file_id)
        out["graph"] = True
    except Exception as e:
        logger.warning(f"purge graph {file_id}: {e}")
    try:
        purge_file_bodies(vector_store_id, file_id)
        out["curation"] = True
    except Exception as e:
        logger.warning(f"purge curation {file_id}: {e}")
    return out


def exclude_file(
    vector_store_id: str,
    file_id: str,
    *,
    reason: Optional[str] = None,
    excluded_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Marca EXCLUDED il file (per file_id). Cattura lo sharepoint_file_id + filename
    dal job più recente (così la sync lo riconosce in futuro), porta i suoi job a
    EXCLUDED e ripulisce i dati già indicizzati. Idempotente (upsert)."""
    job = ingestion_jobs.find_one(
        {"vector_store_id": vector_store_id, "file_id": file_id},
        sort=[("created_at", -1)],
    )
    attrs = (job or {}).get("attributes") or {}
    sp_id = attrs.get("sharepoint_file_id")
    filename = (job or {}).get("filename")

    now = get_timestamp()
    doc = {
        "vector_store_id": vector_store_id,
        "file_id": file_id,
        "sharepoint_file_id": sp_id,
        "filename": filename,
        "reason": reason,
        "excluded_by": excluded_by,
        "excluded_at": now,
    }
    excluded_coll.update_one(
        {"vector_store_id": vector_store_id, "file_id": file_id},
        {"$set": doc},
        upsert=True,
    )

    # i job del file → EXCLUDED: spariscono da failed/pending, il worker non li pesca
    # (claim cerca solo PENDING) e i retry non li toccano (cercano solo FAILED).
    upd = ingestion_jobs.update_many(
        {"vector_store_id": vector_store_id, "file_id": file_id},
        {"$set": {"status": "EXCLUDED", "error": None, "updated_at": now}},
    )

    purged = _purge_file_data(vector_store_id, file_id)
    logger.info(
        f"escluso file {file_id} (sp={sp_id}) da {vector_store_id} — "
        f"job→EXCLUDED={getattr(upd, 'modified_count', 0)} purge={purged}"
    )
    return {**doc, "jobs_excluded": getattr(upd, "modified_count", 0), "purged": purged}


def unexclude_file(vector_store_id: str, file_id: str) -> bool:
    """Toglie l'esclusione (per file_id). I job restano EXCLUDED: per re-ingestire si
    re-attacca (manuale) o si re-sincronizza (SharePoint, che ora non lo salta più).
    Ritorna True se esisteva un'esclusione."""
    res = excluded_coll.delete_one({"vector_store_id": vector_store_id, "file_id": file_id})
    if res.deleted_count:
        logger.info(f"riammesso file {file_id} in {vector_store_id}")
    return res.deleted_count > 0
