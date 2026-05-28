"""
Schema di ingestion con CASCADE multi-livello.

Governa, per-campo e a cascata, sia il CHUNKING (chunk_max_tokens) sia l'ESTRAZIONE
zero-shot (entità/relazioni GLiNER/GLiNER-relex, che sono solo liste di label). Lo
schema EFFETTIVO di un file si risolve dal livello più specifico al più generico:

    file → directory (slug) → sync (job) → store → default globale

Così l'admin configura dove ha senso: sull'intero store, su una cartella, su una
specifica sync (una connessione può alimentare cartelle diverse), o sul singolo file.
Un livello che non definisce un campo lo eredita da quello sotto (es. il file
sovrascrive solo chunk_max_tokens e tiene le entità della directory).

Storage: MongoDB `extraction_schemas` (nome storico; ora copre anche il chunking),
un doc per scope impostato:
    { _id: "<scope>:<ident>", scope, vector_store_id,
      chunk_max_tokens?, entity_labels?, relation_labels?, relations_enabled? }
scope ∈ {store, dir, sync, file}; ident = vs_id | "vs_id:slug" | sync_job_id | file_id.
Un campo assente nel doc significa "eredita" (non sovrascrive). Tutto best-effort: se
Mongo non risponde si cade sui default globali, l'ingestion non si blocca.
"""

from typing import Any, Dict, List, Optional

from utils.database import db
from utils.logger import get_logger
from utils.settings import (
    GLINER_LABELS,
    RELATIONS_LABELS,
    RELATIONS_ENABLED,
    PARSER_MODEL_MAX_TOKENS,
)

logger = get_logger(__name__)

_coll = db["extraction_schemas"]
_FIELDS = ("chunk_max_tokens", "entity_labels", "relation_labels", "relations_enabled")
_SCOPES = ("store", "dir", "sync", "file")

# Limiti di chunk_max_tokens. Solo VINCOLI TECNICI, non opinioni di qualità: chunk
# grandi possono essere una scelta legittima dell'utente (non lo sappiamo a priori).
#  - floor: un minimo di sanità (sotto è inutile / rompe il chunker).
#  - cap: la finestra del tokenizer/embedding BGE-M3. Oltre, il chunk verrebbe
#    TRONCATO in fase di embedding → testo perso in silenzio, meglio rifiutare.
CHUNK_MAX_TOKENS_MIN = 16
CHUNK_MAX_TOKENS_MAX = 8192

try:
    _coll.create_index("vector_store_id")  # per il cleanup al delete dello store
except Exception as _e:  # pragma: no cover
    logger.warning(f"extraction_schemas index setup failed: {_e}")


def _doc_id(scope: str, ident: str) -> str:
    return f"{scope}:{ident}"


def _global_defaults() -> Dict[str, Any]:
    return {
        "chunk_max_tokens": int(PARSER_MODEL_MAX_TOKENS),
        "entity_labels": list(GLINER_LABELS),
        "relation_labels": list(RELATIONS_LABELS),
        "relations_enabled": bool(RELATIONS_ENABLED),
    }


def get_effective_schema(
    vector_store_id: str,
    directory_slug: Optional[str] = None,
    sync_id: Optional[str] = None,
    file_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Schema EFFETTIVO per un file: cascata file→dir→sync→store→default, per-campo.
    Ritorna sempre {entity_labels, relation_labels, relations_enabled} valorizzati."""
    # candidati dal più specifico al più generico
    candidates: List[str] = []
    if file_id:
        candidates.append(_doc_id("file", file_id))
    if directory_slug:
        candidates.append(_doc_id("dir", f"{vector_store_id}:{directory_slug}"))
    if sync_id:
        candidates.append(_doc_id("sync", sync_id))
    candidates.append(_doc_id("store", vector_store_id))

    eff = _global_defaults()
    try:
        docs = {d["_id"]: d for d in _coll.find({"_id": {"$in": candidates}})}
    except Exception as e:
        logger.warning(f"get_effective_schema({vector_store_id}) failed, uso i default: {e}")
        return eff

    # applica dal meno specifico al più specifico → il più specifico vince
    for cid in reversed(candidates):
        d = docs.get(cid)
        if not d:
            continue
        for f in _FIELDS:
            if d.get(f) is not None:
                eff[f] = d[f]
    return eff


def get_schema_doc(scope: str, ident: str) -> Optional[Dict[str, Any]]:
    """Schema impostato a QUESTO specifico livello (None se non impostato).
    Serve alla UI per mostrare cosa è override locale vs ereditato."""
    try:
        d = _coll.find_one({"_id": _doc_id(scope, ident)})
    except Exception:
        d = None
    if not d:
        return None
    return {f: d.get(f) for f in _FIELDS}


def set_schema(
    scope: str,
    ident: str,
    vector_store_id: str,
    entity_labels: Optional[List[str]] = None,
    relation_labels: Optional[List[str]] = None,
    relations_enabled: Optional[bool] = None,
    chunk_max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Imposta (upsert) lo schema a un livello. Campi None = non sovrascrivere (eredita).
    `vector_store_id` serve per il cleanup al delete dello store.

    NB: chunk_max_tokens vale dal prossimo (re-)ingest dei documenti — non ri-chunka
    quelli già indicizzati."""
    if scope not in _SCOPES:
        raise ValueError(f"scope non valido: {scope}")
    update: Dict[str, Any] = {"scope": scope, "vector_store_id": vector_store_id}
    if entity_labels is not None:
        update["entity_labels"] = [s.strip() for s in entity_labels if s and s.strip()]
    if relation_labels is not None:
        update["relation_labels"] = [s.strip() for s in relation_labels if s and s.strip()]
    if relations_enabled is not None:
        update["relations_enabled"] = bool(relations_enabled)
    if chunk_max_tokens is not None:
        ct = int(chunk_max_tokens)
        if not CHUNK_MAX_TOKENS_MIN <= ct <= CHUNK_MAX_TOKENS_MAX:
            raise ValueError(
                f"chunk_max_tokens deve essere tra {CHUNK_MAX_TOKENS_MIN} e {CHUNK_MAX_TOKENS_MAX}"
            )
        update["chunk_max_tokens"] = ct
    _coll.update_one({"_id": _doc_id(scope, ident)}, {"$set": update}, upsert=True)
    return get_schema_doc(scope, ident) or {}


def delete_schema_doc(scope: str, ident: str) -> None:
    """Rimuove l'override a un livello (torna a ereditare)."""
    try:
        _coll.delete_one({"_id": _doc_id(scope, ident)})
    except Exception as e:
        logger.warning(f"delete_schema_doc({scope}:{ident}) failed: {e}")


def delete_store_schemas(vector_store_id: str) -> None:
    """Rimuove TUTTI gli schemi (ogni scope) di uno store — al delete dello store."""
    try:
        _coll.delete_many({"vector_store_id": vector_store_id})
    except Exception as e:
        logger.warning(f"delete_store_schemas({vector_store_id}) failed: {e}")
