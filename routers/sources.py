"""Ingestion sources endpoints.

Una source è una connessione configurabile (SharePoint, Google Drive, S3, ...)
con le proprie credenziali. Il tipo è gestito da un provider (utils/sources):
ogni provider dichiara i campi di config, quali sono secret (cifrati at-rest) e
come navigare la sorgente (browse). I secret non vengono mai restituiti.
"""

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from utils import get_logger, get_timestamp, generate_id
from utils.database import db
from utils.crypto import encrypt_secret
from utils.schemas import SourceCreate, SourceUpdate, SourceResponse
from utils.sources import get_provider, list_providers

logger = get_logger(__name__)

sources_coll = db["ingestion_sources"]

router = APIRouter(prefix="/v1/sources", tags=["Sources"])


def _to_response(doc: dict) -> SourceResponse:
    """Response pubblica: niente secret (solo `secret_set`)."""
    provider = get_provider(doc.get("type", ""))
    secret_fields = provider.secret_fields() if provider else ["client_secret"]
    cfg = dict(doc.get("config") or {})
    secret_set = any(cfg.get(f"{f}_enc") for f in secret_fields)
    public = {k: v for k, v in cfg.items() if not k.endswith("_enc")}
    return SourceResponse(
        id=doc["_id"],
        name=doc.get("name", ""),
        type=doc.get("type", "sharepoint"),
        status=doc.get("status", "active"),
        config=public,
        secret_set=secret_set,
        created_at=doc.get("created_at", 0),
        updated_at=doc.get("updated_at", 0),
    )


def _encrypt_secrets(provider, config: Dict[str, Any]) -> Dict[str, Any]:
    """Cifra i campi secret del provider (`<name>` → `<name>_enc`)."""
    out = dict(config or {})
    for f in provider.secret_fields():
        secret = out.pop(f, None)
        if secret:
            out[f"{f}_enc"] = encrypt_secret(secret)
    return out


@router.get("/types")
async def list_source_types():
    """Provider disponibili (per il form dinamico in UI). enabled=False = 'presto'."""
    return {"object": "list", "data": [p.describe() for p in list_providers()]}


@router.post("", response_model=SourceResponse)
async def create_source(data: SourceCreate):
    """Crea una source. Valida il tipo e i campi richiesti; cifra i secret."""
    provider = get_provider(data.type)
    if not provider:
        raise HTTPException(status_code=422, detail=f"Tipo source sconosciuto: '{data.type}'")
    if not provider.enabled:
        raise HTTPException(status_code=422, detail=f"Provider '{provider.label}' non ancora disponibile")

    cfg_in = data.config or {}
    missing = [f.label for f in provider.config_fields if f.required and not cfg_in.get(f.name)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Campi obbligatori mancanti: {', '.join(missing)}")

    now = get_timestamp()
    doc = {
        "_id": generate_id("src_"),
        "name": data.name,
        "type": data.type,
        "status": "active",
        "config": _encrypt_secrets(provider, cfg_in),
        "created_at": now,
        "updated_at": now,
    }
    await asyncio.to_thread(sources_coll.insert_one, doc)
    return _to_response(doc)


@router.get("")
async def list_sources():
    """Lista tutte le source (senza secret)."""
    docs = await asyncio.to_thread(lambda: list(sources_coll.find().sort("created_at", -1)))
    return {"object": "list", "data": [_to_response(d) for d in docs]}


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(source_id: str):
    doc = await asyncio.to_thread(sources_coll.find_one, {"_id": source_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Source not found")
    return _to_response(doc)


@router.get("/{source_id}/browse")
async def browse_source(
    source_id: str,
    drive_id: str | None = Query(default=None),
    folder_id: str | None = Query(default=None),
):
    """Naviga la sorgente (per il folder picker). Dispatch sul provider della source."""
    doc = await asyncio.to_thread(sources_coll.find_one, {"_id": source_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Source not found")

    provider = get_provider(doc.get("type", ""))
    if not provider:
        raise HTTPException(status_code=422, detail="Provider non disponibile")

    try:
        return await asyncio.to_thread(provider.browse, doc.get("config") or {}, drive_id, folder_id)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        logger.error(f"browse_source({source_id}) failed: {e}")
        raise HTTPException(status_code=502, detail=f"Browse failed: {e}")


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source(source_id: str, data: SourceUpdate):
    doc = await asyncio.to_thread(sources_coll.find_one, {"_id": source_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Source not found")

    update: dict = {"updated_at": get_timestamp()}

    if data.name is not None:
        update["name"] = data.name
    if data.status is not None:
        update["status"] = data.status

    if data.config is not None:
        provider = get_provider(doc.get("type", ""))
        secret_fields = provider.secret_fields() if provider else ["client_secret"]
        new_config = dict(doc.get("config") or {})
        incoming = dict(data.config)
        # Secret: aggiorna solo se forniti, altrimenti mantiene quelli esistenti.
        for f in secret_fields:
            val = incoming.pop(f, None)
            if val:
                new_config[f"{f}_enc"] = encrypt_secret(val)
        new_config.update(incoming)
        update["config"] = new_config

    await asyncio.to_thread(sources_coll.update_one, {"_id": source_id}, {"$set": update})
    doc.update(update)
    return _to_response(doc)


@router.delete("/{source_id}")
async def delete_source(source_id: str):
    result = await asyncio.to_thread(sources_coll.delete_one, {"_id": source_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"id": source_id, "object": "ingestion_source.deleted", "deleted": True}
