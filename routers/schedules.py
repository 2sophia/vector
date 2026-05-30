"""Sync schedule endpoints — cron interno per le sync, configurabile da UI.

Lo schedule è per TIPO di source (es. "sharepoint"): quando scatta, lo scheduler
worker sincronizza tutti i job di quel tipo. I run vengono loggati (ultimi 5).
"""
from typing import Optional

from croniter import croniter
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from utils import get_logger, get_timestamp
from utils.database import db
from utils.scheduling import cron_next_run
from utils.settings import SCHEDULER_TZ

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/sync", tags=["Sync schedule"])

schedules = db["sync_schedules"]
runs = db["sync_runs"]

# Tipi di source che supportano la sync schedulata (allineato a SYNC_ENDPOINTS del worker)
SUPPORTED_TYPES = {"sharepoint"}


class ScheduleUpdate(BaseModel):
    enabled: bool
    cron: str  # espressione cron (es. "0 3 * * *" = 3AM, "*/5 * * * *" = ogni 5 min)


def _serialize(stype: str, doc: Optional[dict]) -> dict:
    doc = doc or {}
    return {
        "type": stype,
        "enabled": bool(doc.get("enabled", False)),
        "cron": doc.get("cron"),
        "next_run": doc.get("next_run"),
        "last_run": doc.get("last_run"),
        "tz": SCHEDULER_TZ,  # in quale fuso è interpretato il cron (per la UI)
    }


@router.get("/schedule/{stype}")
def get_schedule(stype: str):
    if stype not in SUPPORTED_TYPES:
        raise HTTPException(status_code=404, detail=f"Tipo source non supportato: {stype}")
    doc = schedules.find_one({"_id": stype})
    return _serialize(stype, doc)


@router.put("/schedule/{stype}")
def set_schedule(stype: str, body: ScheduleUpdate):
    if stype not in SUPPORTED_TYPES:
        raise HTTPException(status_code=404, detail=f"Tipo source non supportato: {stype}")
    cron = (body.cron or "").strip()
    if body.enabled:
        if not cron or not croniter.is_valid(cron):
            raise HTTPException(status_code=422, detail=f"Espressione cron non valida: '{cron}'")

    now = get_timestamp()
    update = {"enabled": body.enabled, "cron": cron, "updated_at": now}
    if body.enabled and cron:
        update["next_run"] = cron_next_run(cron, now)
    else:
        update["next_run"] = None

    schedules.update_one({"_id": stype}, {"$set": update}, upsert=True)
    return _serialize(stype, schedules.find_one({"_id": stype}))


@router.get("/runs/{stype}")
def list_runs(stype: str, limit: int = Query(5, ge=1, le=50)):
    docs = list(runs.find({"type": stype}).sort("started_at", -1).limit(limit))
    data = [{k: v for k, v in d.items() if k != "_id"} for d in docs]
    return {"object": "list", "data": data}
