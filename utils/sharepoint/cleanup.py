# filepath: sharepoint/cleanup.py
"""
SharePoint Cleanup Processor - Versione Hardened.

Gestisce:
- Locking per evitare esecuzioni concorrenti sullo stesso job
- Cleanup parziale resiliente (riprende da dove si è fermato)
- Memory efficient per grandi volumi di file
- Protezione contro job sovrapposti
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Iterator, Optional

from bson import ObjectId
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from utils import get_timestamp
from utils.database import db
from utils.filesystem import delete_file_from_disk
from utils.logger import get_logger
from utils.qdrant import delete_qdrant_points

from .ingestion import (
    GraphAPIClient,
    SharePointAuth,
    IngestConfig,
    FolderConfig,
    Rule,
    RESERVED_ATTR_KEYS,
    resolve_source_auth,
    AZURE_SITE_URL,
    AZURE_TENANT_ID,
    AZURE_CLIENT_ID,
    AZURE_CLIENT_SECRET,
)

logger = get_logger(__name__)

ingestion_jobs = db["ingestion_jobs"]
sharepoint_jobs = db["sharepoint_jobs"]
cleanup_locks = db["cleanup_locks"]

LOCK_TIMEOUT_MINUTES = 30
BATCH_SIZE = 100

# Costante per throttling
API_THROTTLE_SECONDS = 0.5  # 500ms tra chiamate

class CleanupStats(BaseModel):
    """Statistiche di esecuzione del cleanup."""

    files_checked: int = 0
    files_deleted: int = 0
    files_not_found_in_sharepoint: int = 0
    files_failed: int = 0
    qdrant_points_deleted: int = 0
    disk_files_deleted: int = 0
    ingestion_jobs_deleted: int = 0


class CleanupResult(BaseModel):
    """Risultato dell'operazione di cleanup."""

    success: bool
    sharepoint_job_id: str
    vector_store_id: str
    stats: CleanupStats
    triggered_reingestion: bool = False
    was_locked: bool = False
    error: Optional[str] = None


class CleanupLockError(Exception):
    """Eccezione quando il job è già in fase di cleanup."""

    pass


@asynccontextmanager
async def acquire_cleanup_lock(
    sharepoint_job_id: str,
) -> AsyncGenerator[bool, None]:
    """
    Acquisisce un lock distribuito per il cleanup di un job.

    Usa MongoDB per garantire che solo un processo alla volta
    possa eseguire il cleanup su un determinato job.

    Il lock scade automaticamente dopo LOCK_TIMEOUT_MINUTES.
    """
    lock_id = f"cleanup_{sharepoint_job_id}"
    now = datetime.now(timezone.utc)
    lock_expiry = now + timedelta(minutes=LOCK_TIMEOUT_MINUTES)

    acquired = False
    try:
        # Tentativo atomico: prendi il lock se non esiste o se è scaduto.
        # In caso di race su upsert (DuplicateKeyError), un altro processo
        # ha vinto la corsa.
        try:
            result = await asyncio.to_thread(
                cleanup_locks.update_one,
                {
                    "_id": lock_id,
                    "$or": [
                        {"expires_at": {"$lt": now}},
                        {"expires_at": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "locked_at": now,
                        "expires_at": lock_expiry,
                        "locked_by": f"cleanup_worker_{ObjectId()}",
                    }
                },
                upsert=True,
            )
        except DuplicateKeyError:
            existing = await asyncio.to_thread(cleanup_locks.find_one, {"_id": lock_id})
            raise CleanupLockError(
                f"Job {sharepoint_job_id} is already being cleaned up. "
                f"Lock expires at {existing.get('expires_at') if existing else 'unknown'}"
            )

        if result.modified_count == 0 and result.upserted_id is None:
            existing = await asyncio.to_thread(cleanup_locks.find_one, {"_id": lock_id})
            if existing and existing.get("expires_at", now) > now:
                raise CleanupLockError(
                    f"Job {sharepoint_job_id} is already being cleaned up. "
                    f"Lock expires at {existing.get('expires_at')}"
                )

        acquired = True
        logger.info(f"🔒 Lock acquired for job {sharepoint_job_id}")
        yield True

    finally:
        if acquired:
            await asyncio.to_thread(cleanup_locks.delete_one, {"_id": lock_id})
            logger.info(f"🔓 Lock released for job {sharepoint_job_id}")


@dataclass
class SharePointCleanupProcessor:
    """
    Processor per la pulizia dei file eliminati da SharePoint.

    Caratteristiche:
        - Locking distribuito per evitare esecuzioni concorrenti
        - Iterazione lazy sui file SharePoint (memory efficient)
        - Batch processing per i file indicizzati
        - Protezione contro job con folder sovrapposte
    """

    sharepoint_job_id: str
    _client: Optional[GraphAPIClient] = field(default=None, init=False)
    _config: Optional[IngestConfig] = field(default=None, init=False)
    _job_doc: Optional[dict] = field(default=None, init=False)
    _stats: CleanupStats = field(default_factory=CleanupStats, init=False)

    def _load_sharepoint_job(self) -> dict:
        """Carica il job SharePoint da MongoDB."""
        job_doc = sharepoint_jobs.find_one({"_id": ObjectId(self.sharepoint_job_id)})

        if not job_doc:
            raise ValueError(f"SharePoint job {self.sharepoint_job_id} not found")

        if job_doc.get("status") != "COMPLETED":
            raise ValueError(
                f"SharePoint job {self.sharepoint_job_id} is not COMPLETED "
                f"(current status: {job_doc.get('status')}). Cleanup aborted."
            )

        self._job_doc = job_doc
        return job_doc

    def _build_config_from_job(self) -> IngestConfig:
        """Costruisce IngestConfig dal documento job."""
        job = self._job_doc

        folders = [
            FolderConfig(
                sharepoint_id=f["sharepoint_id"],
                recursive=f.get("recursive", True),
            )
            for f in job.get("folders", [])
        ]

        include_rules = [
            Rule(type=r["type"], value=r["value"])
            for r in job.get("include_rules", [])
        ]

        exclude_rules = [
            Rule(type=r["type"], value=r["value"])
            for r in job.get("exclude_rules", [])
        ]

        self._config = IngestConfig(
            vector_store_id=job["vector_store_id"],
            folders=folders,
            include_rules=include_rules or None,
            exclude_rules=exclude_rules or None,
            attributes=job.get("attributes", {}),
        )

        return self._config

    def _init_graph_client(self) -> GraphAPIClient:
        """Inizializza il client Graph API.

        Usa le credenziali della source referenziata dal job (cifrate in
        ingestion_sources, come fa il worker di sync). Solo se il job non ha una
        source_id si ricade sulle env legacy AZURE_* — che in dev sono vuote e
        causavano un 401 (token URL senza tenant) prima di questo fix.
        """
        source_id = self._job_doc.get("source_id") if self._job_doc else None
        if source_id:
            auth = resolve_source_auth(source_id)
        else:
            auth = SharePointAuth(
                site_url=AZURE_SITE_URL,
                client_id=AZURE_CLIENT_ID,
                client_secret=AZURE_CLIENT_SECRET,
                tenant_id=AZURE_TENANT_ID,
            )
        self._client = GraphAPIClient(auth)
        self._client.connect_to_site()
        return self._client

    def _iter_sharepoint_file_ids(self) -> Iterator[str]:
        """
        Itera sui file ID in SharePoint in modo lazy.

        NON accumula tutto in memoria - yielda un ID alla volta.
        """
        for folder_config in self._config.folders:
            folder_info = self._client.get_folder_info(folder_config.sharepoint_id)

            if not folder_info:
                logger.warning(
                    f"Folder {folder_config.sharepoint_id} not found, skipping..."
                )
                continue

            if "folder" not in folder_info:
                logger.warning(
                    f"Item {folder_config.sharepoint_id} is not a folder, skipping..."
                )
                continue

            drive_id = folder_info.get("_drive_id")

            for file_info in self._client.iter_folder_files(
                drive_id=drive_id,
                folder_id=folder_config.sharepoint_id,
                recursive=folder_config.recursive,
            ):
                yield file_info["id"]

    def _build_sharepoint_file_ids_set(self) -> set[str]:
        """
        Costruisce il set di file ID da SharePoint.

        Per volumi molto grandi (>10k file), considera usare
        un database temporaneo invece di un set in memoria.
        """
        logger.info("Building SharePoint files index...")

        file_ids: set[str] = set()
        count = 0

        for file_id in self._iter_sharepoint_file_ids():
            file_ids.add(file_id)
            count += 1

            if count % 500 == 0:
                logger.info(f"  Indexed {count} files from SharePoint...")

        logger.info(f"SharePoint index built: {len(file_ids)} files")
        return file_ids

    def _iter_indexed_files_batched(self) -> Iterator[list[dict]]:
        """
        Itera sui file indicizzati in batch per efficienza.

        Usa cursor MongoDB con batch_size per non caricare
        tutto in memoria.
        """
        query = {
            "vector_store_id": self._config.vector_store_id,
            "status": "COMPLETED",
            "attributes.sharepoint_file_id": {"$exists": True},
            "sharepoint_job_id": ObjectId(self.sharepoint_job_id),
        }

        for key, value in (self._config.attributes or {}).items():
            if key in RESERVED_ATTR_KEYS:
                continue
            # la chiave diventa un field name Mongo (attributes.<key>): salta quelle
            # con '.' (path annidato) o '$' (operatore) → non sono attributi reali.
            if "." in key or key.startswith("$"):
                logger.warning(f"attributo con chiave non sicura ignorato nel match: {key!r}")
                continue
            query[f"attributes.{key}"] = value

        cursor = ingestion_jobs.find(query).batch_size(BATCH_SIZE)

        batch: list[dict] = []
        for doc in cursor:
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                yield batch
                batch = []

        if batch:
            yield batch

    def _check_overlapping_jobs(self) -> list[str]:
        """
        Verifica se ci sono altri job che usano le stesse folder.

        Returns:
            Lista di job_id che hanno folder in comune.
        """
        my_folder_ids = {f.sharepoint_id for f in self._config.folders}

        overlapping = sharepoint_jobs.find(
            {
                "_id": {"$ne": ObjectId(self.sharepoint_job_id)},
                "status": "COMPLETED",
                "folders.sharepoint_id": {"$in": list(my_folder_ids)},
            },
            {"_id": 1, "folders": 1},
        )

        overlapping_jobs = []
        for job in overlapping:
            job_folder_ids = {f["sharepoint_id"] for f in job.get("folders", [])}
            common = my_folder_ids & job_folder_ids
            if common:
                overlapping_jobs.append(str(job["_id"]))
                logger.warning(
                    f"⚠️ Job {job['_id']} shares folders with current job: {common}"
                )

        return overlapping_jobs

    async def _delete_file_resources(self, job_doc: dict) -> bool:
        """
        Elimina tutte le risorse associate a un file.

        Ordine di eliminazione (fail-safe):
            1. Qdrant (può essere rifatto)
            2. Disco (può essere rifatto)
            3. MongoDB (punto di non ritorno)
        """
        job_id = job_doc["_id"]
        file_id = job_doc.get("file_id")
        sharepoint_file_id = job_doc.get("attributes", {}).get("sharepoint_file_id")
        filename = job_doc.get("filename", "unknown")

        logger.info(f"🗑️ Deleting: {filename} (job_id={job_id})")

        try:
            if sharepoint_file_id:
                await asyncio.to_thread(
                    delete_qdrant_points,
                    self._config.vector_store_id,
                    "sharepoint_file_id",
                    sharepoint_file_id,
                )
                self._stats.qdrant_points_deleted += 1

            if file_id:
                await delete_file_from_disk(file_id=file_id)
                self._stats.disk_files_deleted += 1

            await asyncio.to_thread(ingestion_jobs.delete_one, {"_id": job_id})
            self._stats.ingestion_jobs_deleted += 1

            logger.info(f"  ✓ Deleted successfully")
            return True

        except Exception as e:
            logger.exception(f"  ✗ Failed to delete {filename}: {e}")
            self._stats.files_failed += 1
            return False

    def _log_cleanup_complete(self) -> None:
        """Aggiorna il job con le statistiche finali."""
        now_ts = get_timestamp()
        sharepoint_jobs.update_one(
            {"_id": ObjectId(self.sharepoint_job_id)},
            {
                "$set": {
                    "updated_at": now_ts,
                    "last_cleanup_at": now_ts,
                    "last_cleanup_stats": self._stats.model_dump(),
                }
            },
        )

    async def run(self) -> CleanupResult:
        """Esegue il processo di cleanup con locking."""
        logger.info(f"\n{'=' * 60}")
        logger.info("SHAREPOINT CLEANUP START")
        logger.info(f"Job ID: {self.sharepoint_job_id}")
        logger.info(f"{'=' * 60}\n")

        try:
            async with acquire_cleanup_lock(self.sharepoint_job_id):
                return await self._run_cleanup_logic()

        except CleanupLockError as e:
            logger.warning(f"Cleanup skipped: {e}")
            return CleanupResult(
                success=False,
                sharepoint_job_id=self.sharepoint_job_id,
                vector_store_id="",
                stats=self._stats,
                was_locked=True,
                error=str(e),
            )

    async def _run_cleanup_logic(self) -> CleanupResult:
        """Logica principale del cleanup (chiamata con lock acquisito)."""
        try:
            await asyncio.to_thread(self._load_sharepoint_job)
            self._build_config_from_job()
            await asyncio.to_thread(self._init_graph_client)

            overlapping = await asyncio.to_thread(self._check_overlapping_jobs)
            if overlapping:
                logger.warning(
                    f"Found {len(overlapping)} overlapping jobs. "
                    f"Proceeding with caution..."
                )

            # Scan Graph API per costruire l'indice dei file presenti (HTTP intensive)
            sharepoint_file_ids = await asyncio.to_thread(self._build_sharepoint_file_ids_set)

            files_to_delete: list[dict] = []

            # Iter cursor Mongo materializzato in thread per non bloccare l'event loop
            batches = await asyncio.to_thread(lambda: list(self._iter_indexed_files_batched()))
            for batch in batches:
                for job_doc in batch:
                    self._stats.files_checked += 1

                    sharepoint_file_id = job_doc.get("attributes", {}).get(
                        "sharepoint_file_id"
                    )
                    filename = job_doc.get("filename", "unknown")

                    if not sharepoint_file_id:
                        logger.warning(
                            f"Job {job_doc['_id']} has no sharepoint_file_id"
                        )
                        continue

                    if sharepoint_file_id not in sharepoint_file_ids:
                        logger.info(f"📭 Not in SharePoint: {filename}")
                        self._stats.files_not_found_in_sharepoint += 1
                        files_to_delete.append(job_doc)

            logger.info(f"\nFiles to delete: {len(files_to_delete)}")

            for job_doc in files_to_delete:
                success = await self._delete_file_resources(job_doc)
                if success:
                    self._stats.files_deleted += 1

            triggered = self._stats.files_deleted > 0

            await asyncio.to_thread(self._log_cleanup_complete)

            logger.info(f"\n{'=' * 60}")
            logger.info("CLEANUP SUMMARY")
            logger.info(f"{'=' * 60}")
            logger.info(f"Files checked: {self._stats.files_checked}")
            logger.info(f"Files not found: {self._stats.files_not_found_in_sharepoint}")
            logger.info(f"Files deleted: {self._stats.files_deleted}")
            logger.info(f"Files failed: {self._stats.files_failed}")
            logger.info(f"Triggered re-ingestion: {triggered}")

            return CleanupResult(
                success=True,
                sharepoint_job_id=self.sharepoint_job_id,
                vector_store_id=self._config.vector_store_id,
                stats=self._stats,
                triggered_reingestion=triggered,
            )

        except ValueError as e:
            logger.error(f"Cleanup aborted: {e}")
            return CleanupResult(
                success=False,
                sharepoint_job_id=self.sharepoint_job_id,
                vector_store_id=(
                    self._job_doc.get("vector_store_id", "") if self._job_doc else ""
                ),
                stats=self._stats,
                error=str(e),
            )

        except Exception as e:
            logger.exception(f"Cleanup failed: {e}")
            return CleanupResult(
                success=False,
                sharepoint_job_id=self.sharepoint_job_id,
                vector_store_id=(
                    self._job_doc.get("vector_store_id", "") if self._job_doc else ""
                ),
                stats=self._stats,
                error=str(e),
            )


async def run_cleanup(sharepoint_job_id: str) -> CleanupResult:
    """Entry point per eseguire il cleanup."""
    processor = SharePointCleanupProcessor(sharepoint_job_id=sharepoint_job_id)
    return await processor.run()


async def run_all_cleanups() -> list[CleanupResult]:
    """
    Esegue il cleanup su TUTTI i job COMPLETED.

    Utile per cron job schedulato.
    """
    completed_jobs = sharepoint_jobs.find(
        {"status": "COMPLETED"},
        {"_id": 1},
    )

    results = []
    for job in completed_jobs:
        job_id = str(job["_id"])
        logger.info(f"\n>>> Processing job: {job_id}")
        result = await run_cleanup(job_id)
        results.append(result)

    return results