"""
SharePoint ingestion worker.

Polling loop: pesca job PENDING da MongoDB (`sharepoint_jobs`), esegue lo
scan delle cartelle SharePoint, scarica i file e crea i corrispondenti
`ingestion_jobs` PENDING che verranno presi in carico dal vector worker.

Garantisce claim atomico PENDING → PROCESSING (concurrent-safe).
"""

import logging
from typing import List, Dict, Any

import asyncio

from utils.globals import get_timestamp
from utils.database import db
from utils.settings import (
    INGEST_MAX_CONCURRENT_JOBS as MAX_CONCURRENT_JOBS,
    SHAREPOINT_POLL_INTERVAL as POLL_INTERVAL,
)

from utils.sharepoint.ingestion import SharePointProcessor, IngestConfig, resolve_source_auth

# -------------------------------------------------------
# Config & logger
# -------------------------------------------------------

jobs_coll = db["sharepoint_jobs"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sharepoint-worker")


# -------------------------------------------------------
# MongoDB helpers
# -------------------------------------------------------

async def claim_pending_jobs(limit: int) -> List[Dict[str, Any]]:
    """
    Trova fino a `limit` job PENDING e li riserva atomicamente impostando
    status=PROCESSING. Se un altro worker ha già preso il job, il
    find_one_and_update ritorna None e quel job viene saltato.
    """

    def _claim():
        claimed = []
        cursor = jobs_coll.find(
            {"status": "PENDING"},
            sort=[("created_at", 1)],
            limit=limit,
        )
        for job in cursor:
            result = jobs_coll.find_one_and_update(
                {"_id": job["_id"], "status": "PENDING"},
                {"$set": {"status": "PROCESSING", "updated_at": get_timestamp()}},
                return_document=True,
            )
            if result:
                claimed.append(result)
        return claimed

    return await asyncio.to_thread(_claim)


async def update_job(job_id, update: Dict[str, Any]):
    def _update():
        logger.info(f"# update_job, job_id={job_id}")
        jobs_coll.update_one({"_id": job_id}, {"$set": update})

    await asyncio.to_thread(_update)


# -------------------------------------------------------
# Core: processing di un singolo job
# -------------------------------------------------------

async def handle_job(job: Dict[str, Any]):
    job_id = job["_id"]
    job_id_str = str(job_id)
    site_id = job.get("site_id")

    logger.info(f"# handle_job, job_id={job_id_str}, site_id={site_id}")

    if not site_id:
        logger.warning(f"Job {job_id_str} in PROCESSING ma senza site_id → FAILED")
        return await update_job(job_id, {
            "status": "FAILED",
            "error": "Missing site_id",
            "updated_at": get_timestamp(),
        })

    try:
        source_id = job.get("source_id")
        config = IngestConfig(
            vector_store_id=job["vector_store_id"],
            source_id=source_id,
            folders=job["folders"],
            include_rules=job.get("include_rules", []),
            exclude_rules=job.get("exclude_rules", []),
            attributes=job.get("attributes", {}),
        )

        # Credenziali dalla source del job, altrimenti fallback env (auth=None nel processor)
        auth = await asyncio.to_thread(resolve_source_auth, source_id) if source_id else None
        processor = SharePointProcessor(config=config, sharepoint_job_id=job_id, auth=auth)
        result = await processor.process_folders()

        if not result['success']:
            logger.warning(f"SharePoint job {job_id_str} ended with success=False → FAILED")
            return await update_job(job_id, {
                "status": "FAILED",
                "error": "SharePoint processing failed",
                "updated_at": get_timestamp(),
            })

        await update_job(job_id, {
            "status": "COMPLETED",
            "folders_processed": result['folders_processed'],
            "total_files": result['total_files'],
            "processed_files": result['processed_files'],
            "skipped_files": result['skipped_files'],
            "files_failed": result['files_failed'],
            "total_size": result['total_size'],
            "updated_at": get_timestamp(),
        })

        logger.info(f"✅ Job {job_id_str} COMPLETED")

    except Exception as e:
        logger.exception(f"❌ SharePoint processing failed for job {job_id_str}: {e}")
        return await update_job(job_id, {
            "status": "FAILED",
            "error": f"SharePoint processing failed: {e}",
            "updated_at": get_timestamp(),
        })


async def main_loop():
    logger.info(
        f"🧵 SharePoint ingestion worker started | "
        f"concurrent_jobs={MAX_CONCURRENT_JOBS} poll={POLL_INTERVAL}s"
    )

    while True:
        try:
            jobs = await claim_pending_jobs(MAX_CONCURRENT_JOBS)

            if jobs:
                logger.info(f"🔎 Claimed {len(jobs)} job(s), processing...")
                tasks = [asyncio.create_task(handle_job(job)) for job in jobs]
                await asyncio.gather(*tasks)
                continue  # subito al prossimo poll se c'è ancora coda

        except Exception as e:
            logger.exception(f"⚠️ Unhandled error in main loop: {e}")
            await asyncio.sleep(5.0)
            continue

        # Coda vuota → poll lento
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
