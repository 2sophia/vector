# filepath: routers/ingest.py
"""SharePoint Ingest Endpoint usando Microsoft Graph API"""
import asyncio
import traceback
from datetime import datetime
from threading import Lock

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from utils import get_timestamp
from utils.database import db
from utils.logger import get_logger
from utils.qdrant import delete_qdrant_points
from utils.falkor import purge_file_graph
from utils.curation import purge_file_bodies
from utils.filesystem import delete_file_from_disk
from utils.schemas import StoreSchemaUpdate
from utils.store_schema import (
    set_schema, get_schema_doc, get_effective_schema, delete_schema_doc,
)
from utils.sharepoint.cleanup import run_cleanup, CleanupStats
from utils.sharepoint.ingestion import (
    IngestConfig,
    GraphAPIClient,
    resolve_source_auth,
    default_env_auth,
    IngestResponse,
    IngestStatusResponse,
)

logger = get_logger(__name__)

sharepoint_jobs = db["sharepoint_jobs"]
ingestion_jobs = db["ingestion_jobs"]

# Task in memory - vive solo durante il cleanup
_sync_tasks: dict[str, dict] = {}
_tasks_lock = Lock()

router = APIRouter(prefix="/v1/ingest", tags=["Ingest"])


# ================== MODELS ==================


class SyncResult(BaseModel):
    """Risultato di una sync SharePoint."""
    success: bool
    sharepoint_job_id: str
    vector_store_id: str
    cleanup_stats: CleanupStats | None = None
    reingestion_triggered: bool
    previous_status: str | None = None
    error: str | None = None


class SyncAllResult(BaseModel):
    """Risultato di sync su tutti i job."""
    total_jobs: int
    synced: int
    failed: int
    skipped: int
    results: list[SyncResult]


class SyncTaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class SyncTaskStatus(BaseModel):
    task_id: str
    ingestion_id: str
    status: str  # PENDING, RUNNING, COMPLETED, FAILED
    result: dict | None = None
    error: str | None = None


# ================== HELPER ENDPOINT ==================

async def _sync_job(ingestion_id: str, skip_cleanup: bool = False) -> SyncResult:
    """
    Esegue sync completa di un job SharePoint.

    1. Cleanup: rimuove file eliminati da SharePoint
    2. Re-ingestion: SEMPRE triggera per catturare nuovi/modificati/errori
    """
    try:
        job_oid = ObjectId(ingestion_id)
    except InvalidId:
        return SyncResult(
            success=False,
            sharepoint_job_id=ingestion_id,
            vector_store_id="",
            reingestion_triggered=False,
            error="ingestion_id non valido",
        )

    # Fetch job
    job = await asyncio.to_thread(
        lambda: sharepoint_jobs.find_one({"_id": job_oid})
    )

    if not job:
        return SyncResult(
            success=False,
            sharepoint_job_id=ingestion_id,
            vector_store_id="",
            reingestion_triggered=False,
            error="Job non trovato",
        )

    vector_store_id = job.get("vector_store_id", "")
    previous_status = job.get("status")

    # Skip se già in elaborazione
    if previous_status in ("PENDING", "PROCESSING"):
        return SyncResult(
            success=True,
            sharepoint_job_id=ingestion_id,
            vector_store_id=vector_store_id,
            reingestion_triggered=False,
            previous_status=previous_status,
            error=f"Job già in stato {previous_status}, skip",
        )

    cleanup_stats = None

    try:
        # Step 1: Cleanup (opzionale)
        if not skip_cleanup:
            cleanup_result = await run_cleanup(ingestion_id)
            cleanup_stats = cleanup_result.stats

            if cleanup_result.was_locked:
                return SyncResult(
                    success=False,
                    sharepoint_job_id=ingestion_id,
                    vector_store_id=vector_store_id,
                    cleanup_stats=cleanup_stats,
                    reingestion_triggered=False,
                    previous_status=previous_status,
                    error="Cleanup già in corso (locked)",
                )

        # Step 2: SEMPRE triggera re-ingestion
        update_result = await asyncio.to_thread(
            lambda: sharepoint_jobs.update_one(
                {"_id": job_oid},
                {
                    "$set": {
                        "status": "PENDING",
                        "updated_at": get_timestamp(),
                        "sync_triggered_at": datetime.utcnow(),
                        "last_cleanup_stats": cleanup_stats.model_dump() if cleanup_stats else None,
                    }
                },
            )
        )

        reingestion_triggered = update_result.modified_count > 0

        return SyncResult(
            success=True,
            sharepoint_job_id=ingestion_id,
            vector_store_id=vector_store_id,
            cleanup_stats=cleanup_stats,
            reingestion_triggered=reingestion_triggered,
            previous_status=previous_status,
        )

    except Exception as e:
        logger.error(f"Sync failed for {ingestion_id}: {e}")
        logger.error(traceback.format_exc())

        return SyncResult(
            success=False,
            sharepoint_job_id=ingestion_id,
            vector_store_id=vector_store_id,
            cleanup_stats=cleanup_stats,
            reingestion_triggered=False,
            previous_status=previous_status,
            error=str(e),
        )


async def _run_sync_background(task_id: str, ingestion_id: str, skip_cleanup: bool) -> None:
    """Background task: cleanup → set PENDING → rimuovi da memory."""
    try:
        # Set RUNNING
        with _tasks_lock:
            if task_id in _sync_tasks:
                _sync_tasks[task_id]["status"] = "RUNNING"

        # Esegui sync
        result = await _sync_job(ingestion_id, skip_cleanup)

        # Set result (per chi sta ancora pollando)
        with _tasks_lock:
            if task_id in _sync_tasks:
                _sync_tasks[task_id]["status"] = "COMPLETED" if result.success else "FAILED"
                _sync_tasks[task_id]["result"] = result.model_dump()
                _sync_tasks[task_id]["error"] = result.error

        # Aspetta 60s poi cancella (grace period per polling)
        await asyncio.sleep(60)

    finally:
        # Cleanup memory
        with _tasks_lock:
            _sync_tasks.pop(task_id, None)


# ================== SYNC ENDPOINT ==================

@router.get("/sharepoint/sync/status/{task_id}", response_model=SyncTaskStatus)
async def get_sync_status(task_id: str):
    """
    Poll status di un sync task.

    Il task esiste in memory solo durante il cleanup + 60s dopo.
    Se non trovato, il sync è completato da tempo o mai esistito.
    """
    with _tasks_lock:
        task = _sync_tasks.get(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task non trovato (completato o mai esistito)",
        )

    return SyncTaskStatus(**task)


@router.post("/sharepoint/sync")
async def sync_all_sharepoint_jobs(
        background_tasks: BackgroundTasks,
        skip_cleanup: bool = Query(default=False),
        include_failed: bool = Query(default=True),
):
    """Sync tutti i job. Ritorna lista di task_id."""
    statuses = ["COMPLETED"]
    if include_failed:
        statuses.append("FAILED")

    jobs = await asyncio.to_thread(
        lambda: list(sharepoint_jobs.find({"status": {"$in": statuses}}, {"_id": 1}))
    )

    tasks = []

    for job in jobs:
        ingestion_id = str(job["_id"])
        task_id = f"sync_{ingestion_id}_{int(datetime.utcnow().timestamp())}"

        with _tasks_lock:
            # Skip se già in corso
            existing = next(
                (t for t in _sync_tasks.values()
                 if t["ingestion_id"] == ingestion_id and t["status"] in ("PENDING", "RUNNING")),
                None
            )
            if existing:
                tasks.append({"task_id": existing["task_id"], "ingestion_id": ingestion_id, "skipped": True})
                continue

            _sync_tasks[task_id] = {
                "task_id": task_id,
                "ingestion_id": ingestion_id,
                "status": "PENDING",
                "result": None,
                "error": None,
            }

        background_tasks.add_task(_run_sync_background, task_id, ingestion_id, skip_cleanup)
        tasks.append({"task_id": task_id, "ingestion_id": ingestion_id, "skipped": False})

    return {"total": len(tasks), "tasks": tasks}


@router.post("/sharepoint/{ingestion_id}/sync", response_model=SyncTaskResponse)
async def sync_sharepoint_job(
        ingestion_id: str,
        background_tasks: BackgroundTasks,
        skip_cleanup: bool = Query(default=False),
):
    """
    Sincronizza un job SharePoint (async).

    Ritorna task_id immediato. Poll /sync/status/{task_id} per risultato.
    Il task viene rimosso dalla memory 60s dopo il completamento.
    """
    try:
        ObjectId(ingestion_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ingestion_id non valido")

    # Verifica job esiste
    job = await asyncio.to_thread(
        lambda: sharepoint_jobs.find_one({"_id": ObjectId(ingestion_id)})
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")

    # Verifica non ci sia già un sync in corso per questo job
    with _tasks_lock:
        for task in _sync_tasks.values():
            if task["ingestion_id"] == ingestion_id and task["status"] in ("PENDING", "RUNNING"):
                return SyncTaskResponse(
                    task_id=task["task_id"],
                    status=task["status"],
                    message="Sync già in corso per questo job",
                )

    # Crea task
    task_id = f"sync_{ingestion_id}_{int(datetime.utcnow().timestamp())}"

    with _tasks_lock:
        _sync_tasks[task_id] = {
            "task_id": task_id,
            "ingestion_id": ingestion_id,
            "status": "PENDING",
            "result": None,
            "error": None,
        }

    # Avvia background
    background_tasks.add_task(_run_sync_background, task_id, ingestion_id, skip_cleanup)

    return SyncTaskResponse(
        task_id=task_id,
        status="PENDING",
        message="Sync avviato in background",
    )


# ================== INGEST ENDPOINTS ==================


@router.post("/sharepoint", response_model=IngestResponse)
async def sharepoint_ingest(config: IngestConfig):
    """
    Avvia un job di ingest di documenti da SharePoint usando Microsoft Graph API.

    Le credenziali vengono dalla ingestion source indicata in `source_id`.
    Se `source_id` è assente, fallback alle env globali AZURE_* (legacy).
    """
    try:
        # Credenziali dalla source indicata, altrimenti fallback alle env globali.
        if config.source_id:
            auth = await asyncio.to_thread(resolve_source_auth, config.source_id)
        else:
            auth = default_env_auth()

        client = GraphAPIClient(auth)
        site_id = await asyncio.to_thread(client.connect_to_site)

        now_ts = get_timestamp()

        job_doc = {
            **config.model_dump(),
            "status": "PENDING",
            "error": None,
            "site_id": site_id,
            "created_at": now_ts,
            "updated_at": now_ts,
        }

        result = await asyncio.to_thread(sharepoint_jobs.insert_one, job_doc)

        return {
            "ingestion_id": str(result.inserted_id),
            "site_id": site_id,
            "status": "PENDING",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Endpoint error: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail="Failed to process SharePoint ingest (vedi log del server)",
        )


@router.get("/sharepoint")
async def list_sharepoint_jobs():
    """Lista i job di ingestion SharePoint (più recenti prima)."""
    docs = await asyncio.to_thread(
        lambda: list(sharepoint_jobs.find().sort("created_at", -1).limit(100))
    )
    data = [
        {
            "id": str(d["_id"]),
            "status": d.get("status"),
            "vector_store_id": d.get("vector_store_id"),
            "source_id": d.get("source_id"),
            "attributes": d.get("attributes", {}) or {},
            "folders": d.get("folders", []),
            "total_files": d.get("total_files", 0),
            "processed_files": d.get("processed_files", 0),
            "skipped_files": d.get("skipped_files", 0),
            "files_failed": d.get("files_failed", 0),
            "skipped_files_lists": d.get("skipped_files_lists", []),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }
        for d in docs
    ]
    return {"object": "list", "data": data}


def _sync_schema_response(ingestion_id, vs):
    """own = override impostato a livello sync; effective = schema risolto a cascata
    (sync→store→default). Una sync può alimentare più cartelle: questo schema vale
    per tutti i file che porta, salvo override più specifici a livello directory/file."""
    return {
        "object": "sync.schema",
        "ingestion_id": ingestion_id,
        "vector_store_id": vs,
        "own": get_schema_doc("sync", ingestion_id),
        "effective": get_effective_schema(vs, sync_id=ingestion_id),
    }


@router.get("/sharepoint/{ingestion_id}/schema")
async def get_sync_schema(ingestion_id: str):
    """Schema di estrazione a livello SYNC (override locale + effettivo risolto)."""
    try:
        oid = ObjectId(ingestion_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ingestion_id non valido")
    job = await asyncio.to_thread(sharepoint_jobs.find_one, {"_id": oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return await asyncio.to_thread(_sync_schema_response, ingestion_id, job.get("vector_store_id", ""))


@router.put("/sharepoint/{ingestion_id}/schema")
async def put_sync_schema(ingestion_id: str, body: StoreSchemaUpdate):
    """Override schema a livello sync (campi None = eredita). Vale dal prossimo
    (re-)ingest dei file portati da questa sync."""
    try:
        oid = ObjectId(ingestion_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ingestion_id non valido")
    job = await asyncio.to_thread(sharepoint_jobs.find_one, {"_id": oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    vs = job.get("vector_store_id", "")
    await asyncio.to_thread(
        set_schema, "sync", ingestion_id, vs,
        body.entity_labels, body.relation_labels, body.relations_enabled,
    )
    return await asyncio.to_thread(_sync_schema_response, ingestion_id, vs)


@router.delete("/sharepoint/{ingestion_id}/schema")
async def reset_sync_schema(ingestion_id: str):
    """Rimuove l'override a livello sync → torna a ereditare (store/default)."""
    try:
        ObjectId(ingestion_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ingestion_id non valido")
    await asyncio.to_thread(delete_schema_doc, "sync", ingestion_id)
    job = await asyncio.to_thread(sharepoint_jobs.find_one, {"_id": ObjectId(ingestion_id)})
    return await asyncio.to_thread(_sync_schema_response, ingestion_id, (job or {}).get("vector_store_id", ""))


@router.delete("/sharepoint/{ingestion_id}")
async def delete_sharepoint_job(
        ingestion_id: str,
        purge: bool = Query(default=False),
):
    """Elimina una sync SharePoint (smette di sincronizzare quelle cartelle).

    - purge=False → rimuove solo il job: i file importati restano nella directory.
    - purge=True  → rimuove anche i file importati da questa sync (Qdrant + disco + ingestion_jobs).
    """
    try:
        job_oid = ObjectId(ingestion_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ingestion_id non valido")

    job = await asyncio.to_thread(sharepoint_jobs.find_one, {"_id": job_oid})
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")

    vector_store_id = job.get("vector_store_id", "")
    files_removed = 0

    if purge:
        ing = await asyncio.to_thread(
            lambda: list(ingestion_jobs.find({"sharepoint_job_id": job_oid}))
        )
        for j in ing:
            file_id = j.get("file_id")
            if file_id:
                try:
                    await asyncio.to_thread(delete_qdrant_points, vector_store_id, "file_id", file_id)
                except Exception as e:
                    logger.warning(f"delete_qdrant_points({vector_store_id}, {file_id}) failed: {e}")
                # pulisci anche grafo + curation (best-effort), sennò restano orfani
                await asyncio.to_thread(purge_file_graph, vector_store_id, file_id)
                await asyncio.to_thread(purge_file_bodies, vector_store_id, file_id)
                await delete_file_from_disk(file_id)
            files_removed += 1
        await asyncio.to_thread(ingestion_jobs.delete_many, {"sharepoint_job_id": job_oid})

    await asyncio.to_thread(sharepoint_jobs.delete_one, {"_id": job_oid})
    # rimuovi l'eventuale override schema di questa sync (best-effort)
    await asyncio.to_thread(delete_schema_doc, "sync", ingestion_id)

    return {
        "id": ingestion_id,
        "object": "sharepoint_job.deleted",
        "deleted": True,
        "files_removed": files_removed,
    }


@router.get("/sharepoint/{ingestion_id}", response_model=IngestStatusResponse)
async def get_sharepoint_ingest_status(ingestion_id: str):
    """Ritorna lo stato di un job di ingest SharePoint."""
    try:
        try:
            job_id = ObjectId(ingestion_id)
        except InvalidId:
            raise HTTPException(status_code=400, detail="ingestion_id non valido")

        job = await asyncio.to_thread(sharepoint_jobs.find_one, {"_id": job_id})

        if not job:
            raise HTTPException(status_code=404, detail="Job non trovato")

        status = job.get("status")

        # Controlla se ci sono ancora documenti in elaborazione
        ingestion = await asyncio.to_thread(
            ingestion_jobs.find_one,
            {
                "sharepoint_job_id": job_id,
                "status": {"$in": ["PENDING", "PROCESSING"]},
            },
        )

        if ingestion:
            status = "PROCESSING"

        return {
            "status": status,
            "error": job.get("error"),
            "site_id": job.get("site_id"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "folders_processed": job.get("folders_processed", 0),
            "total_files": job.get("total_files", 0),
            "processed_files": job.get("processed_files", 0),
            "skipped_files": job.get("skipped_files", 0),
            "files_failed": job.get("files_failed", 0),
            "skipped_files_lists": job.get("skipped_files_lists", []),
            "total_size": job.get("total_size", 0),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Status endpoint error: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail="Failed to get SharePoint ingest status (vedi log del server)",
        )
