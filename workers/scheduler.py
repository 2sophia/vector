"""
Scheduler worker — cron interno per le sync delle ingestion source.

Sostituisce il cron di sistema: invece di un crontab cablato, gli schedule sono
in MongoDB (collection `sync_schedules`, uno per tipo source) e configurabili da
UI. Ogni ~SCHEDULER_POLL_INTERVAL secondi controlla quali schedule sono "dovuti"
(next_run passato) e lancia la sync chiamando l'endpoint HTTP del backend stesso
(così riusa tutta la logica + l'overlap guard in-app: se una sync è già RUNNING
non parte un doppione).

Ogni run viene loggato in `sync_runs` (retention: ultimi RUNS_RETENTION per tipo),
così dalla UI si vede cosa ha fatto ogni esecuzione.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from croniter import croniter

from utils import get_timestamp
from utils.database import db
from utils.settings import INTERNAL_API_URL, SCHEDULER_POLL_INTERVAL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

schedules = db["sync_schedules"]
runs = db["sync_runs"]

# tipo source → endpoint di sync (relativo). Estendere per nuovi provider.
SYNC_ENDPOINTS = {
    "sharepoint": "/v1/ingest/sharepoint/sync",
}
RUNS_RETENTION = 5


def _next_run(cron: str, base_ts: int) -> int:
    base = datetime.fromtimestamp(base_ts, tz=timezone.utc)
    return int(croniter(cron, base).get_next())


def _prune_runs(stype: str) -> None:
    """Tiene solo gli ultimi RUNS_RETENTION run del tipo."""
    old = list(
        runs.find({"type": stype}, {"_id": 1}).sort("started_at", -1).skip(RUNS_RETENTION)
    )
    if old:
        runs.delete_many({"_id": {"$in": [d["_id"] for d in old]}})


async def _execute(sch: dict) -> None:
    stype = sch["_id"]
    cron = sch.get("cron") or ""
    endpoint = SYNC_ENDPOINTS.get(stype)
    started = get_timestamp()
    status, detail, error = "OK", None, None

    if not endpoint:
        status, error = "FAILED", f"nessun endpoint sync per tipo '{stype}'"
    else:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                r = await client.post(f"{INTERNAL_API_URL}{endpoint}")
                r.raise_for_status()
                detail = r.json()
            logger.info(f"▶ sync '{stype}' eseguita: {detail}")
        except Exception as e:
            status, error = "FAILED", str(e)
            logger.warning(f"sync '{stype}' fallita: {e}")

    finished = get_timestamp()

    # log del run + retention
    await asyncio.to_thread(
        runs.insert_one,
        {
            "type": stype,
            "started_at": started,
            "finished_at": finished,
            "status": status,
            "detail": detail,
            "error": error,
        },
    )
    await asyncio.to_thread(_prune_runs, stype)

    # ricalcola la prossima esecuzione
    upd = {"last_run": started}
    if cron and croniter.is_valid(cron):
        upd["next_run"] = _next_run(cron, started)
    else:
        # cron invalido/vuoto: senza ricalcolo, next_run resterebbe nel passato e
        # _tick rifarebbe la sync a OGNI tick (busy-loop che martella l'endpoint).
        # Disabilitiamo lo schedule finché qualcuno non corregge il cron dalla UI.
        upd["enabled"] = False
        logger.warning(f"schedule '{stype}': cron invalido/vuoto ({cron!r}) → disabilitato")
    await asyncio.to_thread(schedules.update_one, {"_id": stype}, {"$set": upd})


async def _tick() -> None:
    now = get_timestamp()
    due = await asyncio.to_thread(
        lambda: list(schedules.find({"enabled": True, "next_run": {"$lte": now}}))
    )
    for sch in due:
        await _execute(sch)


async def main_loop() -> None:
    logger.info(f"🗓️  Scheduler started | poll={SCHEDULER_POLL_INTERVAL}s api={INTERNAL_API_URL}")
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception(f"⚠️ scheduler tick error: {e}")
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
