"""Directories endpoints.

Una "directory" è l'astrazione user-facing del prodotto open: raggruppa file
sotto uno slug + custom properties, senza esporre la terminologia interna
(`sophia_directory_slug`, agent, ecc.). Più directory vivono nello stesso
vector store (collezione Qdrant); i file caricati in una directory ereditano
`{sophia_directory_slug: slug, **properties}` come attributi (top-level su Qdrant).
"""

import os
import re
import json
import asyncio

from fastapi import APIRouter, HTTPException
from slugify import slugify

from utils import get_logger, get_timestamp, generate_id
from utils.database import db
from utils.settings import FILES_STORAGE
from utils.qdrant import qdrant_client, create_qdrant_collection, delete_qdrant_points
from utils.falkor import purge_file_graph
from utils.curation import purge_file_bodies
from utils.filesystem import delete_file_from_disk
from utils.schemas import DirectoryCreate, DirectoryUpdate, DirectoryResponse, StoreSchemaUpdate
from utils.store_schema import (
    set_schema, get_schema_doc, get_effective_schema, delete_schema_doc,
)

logger = get_logger(__name__)

directories_coll = db["directories"]
app_config = db["app_config"]
ingestion_jobs = db["ingestion_jobs"]

# Chiave dello slug nel payload (contratto con il consumer). NON esposta in UI.
SLUG_FIELD = "sophia_directory_slug"

# Chiavi property riservate: sono i campi di sistema del payload Qdrant
# (vedi RESERVED_PAYLOAD_KEYS nel vector worker) + lo slug. Una custom property
# non può usarle, altrimenti collide / verrebbe scartata dal worker.
RESERVED_PROP_KEYS = frozenset({
    "job_id", "file_id", "vector_store_id", "filename", "chunk_index",
    "text", "headings", "page_numbers", SLUG_FIELD,
})

router = APIRouter(prefix="/v1/directories", tags=["Directories"])


def normalize_prop_key(raw: str) -> str:
    """Normalizza una chiave property a snake_case [a-z0-9_]."""
    key = str(raw).strip().lower()
    key = re.sub(r"[\s-]+", "_", key)      # spazi/trattini → underscore
    key = re.sub(r"[^a-z0-9_]", "", key)   # via tutto il resto
    key = re.sub(r"_+", "_", key).strip("_")
    return key


def clean_properties(props: dict | None) -> dict:
    """Valida/normalizza le chiavi delle property; rifiuta quelle riservate.

    I valori restano invariati. Chiavi vuote dopo la normalizzazione sono scartate.
    """
    cleaned: dict = {}
    for raw_key, value in (props or {}).items():
        key = normalize_prop_key(raw_key)
        if not key:
            continue
        if key in RESERVED_PROP_KEYS:
            raise HTTPException(
                status_code=422,
                detail=f"Chiave property riservata: '{raw_key}' (normalizzata '{key}')",
            )
        cleaned[key] = value
    return cleaned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qdrant_collections() -> list[str]:
    return [c.name for c in qdrant_client.get_collections().collections]


def get_or_create_default_vector_store() -> str:
    """Ritorna l'id del vector store di default, creandolo se non esiste.

    Modello prodotto: l'utente gestisce directory, non vector store. Le directory
    vivono per default in un'unica collezione "Default". L'id è persistito in
    `app_config` così resta stabile tra i riavvii.
    """
    cfg = app_config.find_one({"_id": "default_vector_store"})
    if cfg and cfg.get("value") in _qdrant_collections():
        return cfg["value"]

    # Crea una nuova collezione di default
    store_id = generate_id("vs_")
    create_qdrant_collection(store_id)

    metadata = {
        "name": "Default",
        "created_at": get_timestamp(),
        "metadata": {"is_default": True},
        "expires_after": None,
    }
    metadata_path = os.path.join(FILES_STORAGE, f"{store_id}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f)

    app_config.update_one(
        {"_id": "default_vector_store"},
        {"$set": {"value": store_id, "updated_at": get_timestamp()}},
        upsert=True,
    )
    logger.info(f"Created default vector store {store_id}")
    return store_id


def _count_files(vector_store_id: str, slug: str) -> int:
    """Numero di file distinti (job) nella directory."""
    return len(
        ingestion_jobs.distinct(
            "file_id",
            {"vector_store_id": vector_store_id, f"attributes.{SLUG_FIELD}": slug},
        )
    )


def _count_unassigned(vector_store_id: str, slugs: list) -> int:
    """File del vector store che NON appartengono a nessuna directory: slug assente,
    vuoto o non corrispondente a una directory esistente. Sono i file caricati via
    API/dev senza `sophia_directory_slug` — validi ma invisibili nelle card directory.
    `$nin` matcha anche i documenti che non hanno proprio il campo (slug mancante)."""
    return len(
        ingestion_jobs.distinct(
            "file_id",
            {"vector_store_id": vector_store_id, f"attributes.{SLUG_FIELD}": {"$nin": slugs}},
        )
    )


def _to_response(doc: dict) -> DirectoryResponse:
    return DirectoryResponse(
        id=doc["_id"],
        name=doc.get("name", ""),
        slug=doc.get("slug", ""),
        properties=doc.get("properties", {}) or {},
        vector_store_id=doc["vector_store_id"],
        file_count=_count_files(doc["vector_store_id"], doc.get("slug", "")),
        created_at=doc.get("created_at", 0),
        updated_at=doc.get("updated_at", 0),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=DirectoryResponse)
async def create_directory(data: DirectoryCreate):
    """Crea una directory. Lo slug è immutabile; default = slugify(name)."""
    # slugify garantisce un formato valido [a-z0-9-]. Vale sia per slug esplicito
    # che derivato dal nome.
    slug = slugify(data.slug or data.name)
    if not slug:
        raise HTTPException(status_code=422, detail="slug/name non valido")
    properties = clean_properties(data.properties)

    vector_store_id = data.vector_store_id
    if not vector_store_id:
        vector_store_id = await asyncio.to_thread(get_or_create_default_vector_store)
    elif vector_store_id not in await asyncio.to_thread(_qdrant_collections):
        raise HTTPException(status_code=404, detail="Vector store not found")

    # slug univoco per vector store (altrimenti i file si mescolerebbero)
    exists = await asyncio.to_thread(
        directories_coll.find_one,
        {"vector_store_id": vector_store_id, "slug": slug},
    )
    if exists:
        raise HTTPException(
            status_code=409,
            detail=f"Directory con slug '{slug}' già esistente in questo vector store",
        )

    now = get_timestamp()
    doc = {
        "_id": generate_id("dir_"),
        "name": data.name,
        "slug": slug,
        "properties": properties,
        "vector_store_id": vector_store_id,
        "created_at": now,
        "updated_at": now,
    }
    await asyncio.to_thread(directories_coll.insert_one, doc)
    return _to_response(doc)


@router.get("")
async def list_directories(vector_store_id: str | None = None):
    """Lista le directory (opzionalmente filtrate per vector store)."""
    query = {"vector_store_id": vector_store_id} if vector_store_id else {}
    docs = await asyncio.to_thread(
        lambda: list(directories_coll.find(query).sort("created_at", -1))
    )
    out = {"object": "list", "data": [_to_response(d) for d in docs]}
    # Bucket "senza directory": i file dello store non assegnati a nessuno slug.
    # Solo quando si filtra per vector store (è un concetto per-store). Permette alla
    # UI una card fissa per i file caricati via API/dev senza slug, altrimenti invisibili.
    if vector_store_id:
        slugs = [d.get("slug", "") for d in docs]
        out["unassigned"] = {
            "vector_store_id": vector_store_id,
            "file_count": await asyncio.to_thread(_count_unassigned, vector_store_id, slugs),
        }
    return out


@router.get("/{directory_id}", response_model=DirectoryResponse)
async def get_directory(directory_id: str):
    doc = await asyncio.to_thread(directories_coll.find_one, {"_id": directory_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Directory not found")
    return _to_response(doc)


def _dir_schema_response(directory_id, vs, slug):
    """own = override impostato a livello directory; effective = schema risolto a
    cascata (dir→sync→store→default). La UI mostra cosa è locale vs ereditato."""
    return {
        "object": "directory.schema",
        "directory_id": directory_id,
        "vector_store_id": vs,
        "slug": slug,
        "own": get_schema_doc("dir", f"{vs}:{slug}"),
        "effective": get_effective_schema(vs, directory_slug=slug),
    }


@router.get("/{directory_id}/schema")
async def get_directory_schema(directory_id: str):
    """Schema di estrazione a livello DIRECTORY (override locale + effettivo risolto)."""
    doc = await asyncio.to_thread(directories_coll.find_one, {"_id": directory_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Directory not found")
    return await asyncio.to_thread(_dir_schema_response, directory_id, doc["vector_store_id"], doc.get("slug", ""))


@router.put("/{directory_id}/schema")
async def put_directory_schema(directory_id: str, body: StoreSchemaUpdate):
    """Override schema a livello directory (campi None = eredita). Vale dal prossimo
    (re-)ingest dei file della directory."""
    doc = await asyncio.to_thread(directories_coll.find_one, {"_id": directory_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Directory not found")
    vs, slug = doc["vector_store_id"], doc.get("slug", "")
    try:
        await asyncio.to_thread(
            set_schema, "dir", f"{vs}:{slug}", vs,
            body.entity_labels, body.relation_labels, body.relations_enabled,
            body.chunk_max_tokens,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return await asyncio.to_thread(_dir_schema_response, directory_id, vs, slug)


@router.delete("/{directory_id}/schema")
async def reset_directory_schema(directory_id: str):
    """Rimuove l'override a livello directory → torna a ereditare (sync/store/default)."""
    doc = await asyncio.to_thread(directories_coll.find_one, {"_id": directory_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Directory not found")
    vs, slug = doc["vector_store_id"], doc.get("slug", "")
    await asyncio.to_thread(delete_schema_doc, "dir", f"{vs}:{slug}")
    return await asyncio.to_thread(_dir_schema_response, directory_id, vs, slug)


@router.patch("/{directory_id}", response_model=DirectoryResponse)
async def update_directory(directory_id: str, data: DirectoryUpdate):
    """Aggiorna name/properties. Lo slug NON è modificabile (spaccherebbe i file)."""
    doc = await asyncio.to_thread(directories_coll.find_one, {"_id": directory_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Directory not found")

    update: dict = {"updated_at": get_timestamp()}
    if data.name is not None:
        update["name"] = data.name
    if data.properties is not None:
        update["properties"] = clean_properties(data.properties)

    await asyncio.to_thread(
        directories_coll.update_one, {"_id": directory_id}, {"$set": update}
    )
    doc.update(update)
    return _to_response(doc)


@router.delete("/{directory_id}")
async def delete_directory(directory_id: str):
    """Elimina la directory e in cascata tutti i suoi file (disco + job + punti Qdrant).

    Cancella SOLO i punti con questo slug: le altre directory dello stesso
    vector store restano intatte.
    """
    doc = await asyncio.to_thread(directories_coll.find_one, {"_id": directory_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Directory not found")

    vector_store_id = doc["vector_store_id"]
    slug = doc.get("slug", "")
    # rimuovi l'eventuale override schema di questa directory (best-effort)
    await asyncio.to_thread(delete_schema_doc, "dir", f"{vector_store_id}:{slug}")
    job_query = {"vector_store_id": vector_store_id, f"attributes.{SLUG_FIELD}": slug}

    # 1) File su disco + grafo + curation (per ogni file_id dei job della directory)
    file_ids = await asyncio.to_thread(ingestion_jobs.distinct, "file_id", job_query)
    for file_id in file_ids:
        await delete_file_from_disk(file_id)
        # pulisci anche grafo + curation (best-effort), sennò restano orfani
        await asyncio.to_thread(purge_file_graph, vector_store_id, file_id)
        await asyncio.to_thread(purge_file_bodies, vector_store_id, file_id)

    # 2) Punti Qdrant con questo slug (best-effort: la collection potrebbe non esistere)
    try:
        await asyncio.to_thread(
            delete_qdrant_points, vector_store_id, SLUG_FIELD, slug
        )
    except Exception as e:
        logger.warning(f"delete_qdrant_points({vector_store_id}, {slug}) failed: {e}")

    # 3) Job Mongo
    jobs_res = await asyncio.to_thread(ingestion_jobs.delete_many, job_query)

    # 4) Record directory
    await asyncio.to_thread(directories_coll.delete_one, {"_id": directory_id})

    return {
        "id": directory_id,
        "object": "directory.deleted",
        "deleted": True,
        "details": {"files_deleted": len(file_ids), "jobs_deleted": jobs_res.deleted_count},
    }
