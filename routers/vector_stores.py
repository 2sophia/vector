"""Vector Store endpoints"""
import asyncio
import os
import json

from typing import Dict, Any, Optional

import aiofiles
import aiofiles.os

from fastapi import HTTPException, Query, APIRouter

from utils.database import db
from utils.docling import PARSER_SUPPORTED_EXTENSIONS
from utils.filesystem import get_file_metadata, get_file_path, delete_file_from_disk
from utils.qdrant import create_qdrant_collection, delete_qdrant_points
from utils.falkor import delete_graph, purge_file_graph

# Import DA UTILS (non serve il punto perché siamo fuori dal pacchetto)
from utils import (
    get_logger,

    qdrant_client,

    get_timestamp,
    generate_id
)

from utils.schemas import (
    VectorStoreCreate,
    FileAttach,
    VectorStore,
    VectorStoreFile,
)

from utils.settings import FILES_STORAGE

logger = get_logger(__name__)

# create collections for jobs async management
ingestion_jobs = db["ingestion_jobs"]
directories_coll = db["directories"]
# ingestion_job_chunks = db["ingestion_job_chunks"]

# Crea il router (come una mini-app)
router = APIRouter(
    prefix="/v1/vector_stores",  # Tutti gli endpoint iniziano con questo
    tags=["Vector Stores"]  # Per la documentazione
)


# ================== VECTOR STORE ENDPOINTS ==================

# TODO: WORK
@router.post("", response_model=VectorStore)
async def create_vector_store(store_data: VectorStoreCreate):
    """Create a new vector store (Qdrant collection)"""
    try:
        store_id = generate_id("vs_")

        # generate first collection for chunk
        await asyncio.to_thread(create_qdrant_collection, store_id)

        # generate another collection for pages
        # await asyncio.to_thread(create_qdrant_collection, f"{store_id}_pages")

        # Store metadata in collection info
        collection_info = {
            "name": store_data.name,
            "created_at": get_timestamp(),
            "metadata": store_data.metadata,
            "expires_after": store_data.expires_after
        }

        # Save metadata to file (since Qdrant doesn't store collection metadata)
        metadata_path = os.path.join(FILES_STORAGE, f"{store_id}_metadata.json")
        async with aiofiles.open(metadata_path, 'w') as f:
            await f.write(json.dumps(collection_info))

        return VectorStore(
            id=store_id,
            name=store_data.name,
            status="completed",
            usage_bytes=0,
            created_at=collection_info["created_at"],
            file_counts={"in_progress": 0, "completed": 0, "failed": 0, "cancelled": 0, "total": 0},
            metadata=store_data.metadata,
            expires_after=store_data.expires_after,
            last_active_at=get_timestamp()
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create vector store: {str(e)}")


# TODO: WORK | REVIEW COUNT FILES
@router.get("", response_model=Dict[str, Any])
async def list_vector_stores(limit: int = Query(20), order: str = Query("desc"), after: Optional[str] = None):
    """List all vector stores"""
    try:
        # Usa un approccio più semplice senza get_collections dettagliato
        try:
            # collections_response = qdrant_client.get_collections()
            collections_response = await asyncio.to_thread(qdrant_client.get_collections)

            collections = collections_response.collections
        except Exception as e:
            import traceback
            logger.error(f"Error in: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Fallback: return empty list
            return {
                "object": "list",
                "data": [],
                "first_id": None,
                "last_id": None,
                "has_more": False
            }

        stores = []

        for collection in collections:
            collection_name = collection.name
            if not collection_name.startswith("vs_"):
                continue

            metadata_path = os.path.join(FILES_STORAGE, f"{collection_name}_metadata.json")

            try:
                async with aiofiles.open(metadata_path, 'r') as f:
                    metadata = json.loads(await f.read())
            except:
                metadata = {"name": collection_name, "created_at": get_timestamp(), "metadata": {}}

            # Usa chiamata più semplice per evitare parsing errors
            try:
                collection_info = await asyncio.to_thread(qdrant_client.get_collection, collection_name)
                point_count = collection_info.points_count if hasattr(collection_info, 'points_count') else 0
            except:
                point_count = 0

            stores.append(VectorStore(
                id=collection_name,
                name=metadata.get("name", collection_name),
                status="completed",
                usage_bytes=point_count * 1024,
                created_at=metadata.get("created_at", get_timestamp()),
                file_counts={"total": point_count, "completed": point_count, "in_progress": 0, "failed": 0,
                             "cancelled": 0},
                metadata=metadata.get("metadata", {}),
                last_active_at=get_timestamp()
            ))

        stores.sort(key=lambda x: x.created_at, reverse=(order == "desc"))

        return {
            "object": "list",
            "data": stores[:limit],
            "first_id": stores[0].id if stores else None,
            "last_id": stores[-1].id if stores else None,
            "has_more": len(stores) > limit
        }

    except Exception as e:
        import traceback
        logger.error(f"Error in: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to list vector stores: {str(e)}")


# TODO: WORK | REVIEW COUNT FILES
@router.get("/{vector_store_id}", response_model=VectorStore)
async def get_vector_store(vector_store_id: str):
    """Get a specific vector store"""
    try:

        if not vector_store_id.startswith("vs_"):
            raise HTTPException(status_code=404, detail="Vector store not found")

        # Check if collection exists
        collections_response = await asyncio.to_thread(qdrant_client.get_collections)
        collections = [c.name for c in collections_response.collections]

        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        metadata_path = os.path.join(FILES_STORAGE, f"{vector_store_id}_metadata.json")

        try:
            async with aiofiles.open(metadata_path, 'r') as f:
                metadata = json.loads(await f.read())
        except:
            metadata = {"name": vector_store_id, "created_at": get_timestamp(), "metadata": {}}

        # collection_info = qdrant_client.get_collection(vector_store_id)
        collection_info = await asyncio.to_thread(qdrant_client.get_collection, vector_store_id)
        point_count = collection_info.points_count

        return VectorStore(
            id=vector_store_id,
            name=metadata.get("name", vector_store_id),
            status="completed",
            usage_bytes=point_count * 1024,
            created_at=metadata.get("created_at", get_timestamp()),
            file_counts={"total": point_count, "completed": point_count, "in_progress": 0, "failed": 0, "cancelled": 0},
            metadata=metadata.get("metadata", {}),
            last_active_at=get_timestamp()
        )

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error in: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to get vector store: {str(e)}")


@router.delete("/{vector_store_id}")
def delete_vector_store(vector_store_id: str):
    """Delete a vector store and all related jobs/files"""
    try:
        if not vector_store_id.startswith("vs_"):
            raise HTTPException(status_code=404, detail="Vector store not found")

        # --- Check if collection exists ---
        collections = [c.name for c in qdrant_client.get_collections().collections]
        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        # --- Delete Qdrant collection ---
        qdrant_client.delete_collection(vector_store_id)

        # --- Delete knowledge graph (best-effort) ---
        delete_graph(vector_store_id)

        # --- Delete vector store metadata file ---
        metadata_path = os.path.join(FILES_STORAGE, f"{vector_store_id}_metadata.json")
        vector_metadata_deleted = False
        if os.path.exists(metadata_path):
            os.remove(metadata_path)
            vector_metadata_deleted = True

        # --- Find all jobs for this vector_store ---
        jobs_cursor = ingestion_jobs.find({"vector_store_id": vector_store_id})

        # Per evitare di cancellare lo stesso file 2 volte:
        file_ids_to_delete = set()
        for job in jobs_cursor:
            file_id = job.get("file_id")
            if file_id:
                file_ids_to_delete.add(file_id)

        # --- Delete all related files + file metadata on disk ---
        deleted_files = []
        for file_id in file_ids_to_delete:
            file_path = os.path.join(FILES_STORAGE, file_id)
            file_metadata_path = os.path.join(FILES_STORAGE, f"{file_id}_metadata.json")

            file_deleted = False
            file_metadata_deleted = False

            if os.path.exists(file_path):
                os.remove(file_path)
                file_deleted = True

            if os.path.exists(file_metadata_path):
                os.remove(file_metadata_path)
                file_metadata_deleted = True

            deleted_files.append({
                "file_id": file_id,
                "file_deleted": file_deleted,
                "file_metadata_deleted": file_metadata_deleted,
            })

        # --- Delete all jobs from Mongo for this vector_store ---
        jobs_delete_result = ingestion_jobs.delete_many({"vector_store_id": vector_store_id})
        jobs_deleted_count = jobs_delete_result.deleted_count

        # --- Delete directories Mongo for this vector_store ---
        dirs_delete_result = directories_coll.delete_many({"vector_store_id": vector_store_id})
        dirs_deleted_count = dirs_delete_result.deleted_count

        # --- Delete job chunks Mongo ---
        # job_delete_chunks_result = ingestion_job_chunks.delete_many({"vector_store_id": vector_store_id})
        # job_chunks_deleted = job_delete_chunks_result.deleted_count > 0

        return {
            "id": vector_store_id,
            "object": "vector_store.deleted",
            "deleted": True,
            "details": {
                "vector_metadata_deleted": vector_metadata_deleted,
                "jobs_deleted": jobs_deleted_count,
                "directories_deleted": dirs_deleted_count,
                "files_deleted": deleted_files,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error in delete_vector_store: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to delete vector store: {str(e)}")


@router.delete("/{vector_store_id}/files/{file_id}")
async def remove_file_from_vector_store(vector_store_id: str, file_id: str):
    """Remove a file from vector store and delete its ingestion job"""
    try:
        # --- Validate input ---
        if not vector_store_id.startswith("vs_"):
            raise HTTPException(status_code=404, detail="Vector store not found")

        if not file_id.startswith("file-"):
            raise HTTPException(status_code=404, detail="File not found")

        # --- Check if collection exists ---
        collections = [c.name for c in qdrant_client.get_collections().collections]
        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        # Usa le utility!
        file_path = await get_file_path(file_id)

        if not file_path:
            raise HTTPException(status_code=404, detail="File not found")

        # --- Delete all Qdrant points with this file_id ---
        delete_qdrant_points(
            collection_name=vector_store_id,
            field_name="file_id",  # <--- chiave dinamica
            field_value=file_id  # <--- valore dinamico
        )

        # Pages Collections || new
        # delete_qdrant_points(
        #     collection_name=f"{vector_store_id}_pages",
        #     field_name="file_id",  # <--- chiave dinamica
        #     field_value=file_id  # <--- valore dinamico
        # )

        # --- Delete file from disk ---
        file_deleted = await delete_file_from_disk(file_id)

        # --- Delete knowledge graph nodes for this file (best-effort) ---
        await asyncio.to_thread(purge_file_graph, vector_store_id, file_id)

        # --- Delete job from Mongo ---
        job_delete_result = ingestion_jobs.delete_one(
            {"vector_store_id": vector_store_id, "file_id": file_id}
        )
        job_deleted = job_delete_result.deleted_count > 0

        # --- Delete job chunks Mongo ---
        # job_delete_chunks_result = ingestion_job_chunks.delete_many(
        #     {"vector_store_id": vector_store_id, "file_id": file_id}
        # )
        # job_chunks_deleted = job_delete_chunks_result.deleted_count > 0

        return {
            "id": f"{vector_store_id}_{file_id}",
            "object": "vector_store.file.deleted",
            "deleted": True,
            "details": {
                "points_deleted": True,
                "file_deleted": file_deleted,
                "job_deleted": job_deleted,
                # "job_chunks_deleted": job_chunks_deleted,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error in remove_file_from_vector_store: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to remove file: {str(e)}")


# TODO: WORK / REVIEW FILE CONTENTS?
@router.post("/{vector_store_id}/files", response_model=VectorStoreFile)
async def attach_file_to_vector_store(vector_store_id: str, file_data: FileAttach):
    """Attach and process a file into vector store"""

    # 1) Check vector_store id
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")

    # Check if vector store exists
    collections_response = await asyncio.to_thread(qdrant_client.get_collections)
    collections = [c.name for c in collections_response.collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")

    # Usa le utility!
    file_path = await get_file_path(file_data.file_id)
    file_metadata = await get_file_metadata(file_data.file_id)

    if not file_path or not file_metadata:
        raise HTTPException(status_code=404, detail="File not found")

    file_ext = os.path.splitext(file_metadata.get("filename", ""))[1].lower()
    if file_ext not in PARSER_SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension '{file_ext}'. Supported: {sorted(PARSER_SUPPORTED_EXTENSIONS)}",
        )

    # async with aiofiles.open(metadata_path, 'r') as f:
    #     file_metadata = json.loads(await f.read())

    # # 2) Upload a Docling (async task)
    # try:
    #     # task_info = upload_file_for_chunking_task_async(file_path)
    #     task_info = await asyncio.to_thread(upload_file_for_chunking_task_async, file_path)
    #
    # except Exception as e:
    #     raise HTTPException(status_code=500, detail=f"Failed to create Docling task: {e}")
    #
    # docling_task_id = task_info["task_id"]
    # docling_status = task_info.get("task_status", "queued")

    # --- Dedup / re-ingest per (directory, filename) ---
    # Un file in una directory è identificato dal nome; lo slug delimita la
    # directory. Stesso contenuto (hash) → duplicato, si salta. Stesso nome ma
    # contenuto diverso → re-ingest sicuro (i vecchi job/punti vengono rimossi
    # SOLO a nuovo ingest COMPLETED, vedi worker: niente delete-before-success).
    content_hash = file_metadata.get("content_hash")
    filename = file_metadata.get("filename")
    attributes = file_data.attributes or {}
    slug = attributes.get("sophia_directory_slug")

    dir_query: Dict[str, Any] = {"vector_store_id": vector_store_id, "filename": filename}
    if slug is not None:
        dir_query["attributes.sophia_directory_slug"] = slug
    existing = await asyncio.to_thread(lambda: list(ingestion_jobs.find(dir_query)))

    # Dedup: stesso contenuto e job non fallito → ritorna l'esistente, niente nuovo job.
    for job in existing:
        same_hash = content_hash and job.get("content_hash") == content_hash
        if same_hash and job.get("status") in ("PENDING", "PROCESSING", "COMPLETED"):
            # Rimuovi il file appena caricato se è un upload nuovo (orfano su disco).
            if job.get("file_id") != file_data.file_id:
                await delete_file_from_disk(file_data.file_id)
            return VectorStoreFile(
                job_id=str(job["_id"]),
                id=job["file_id"],
                usage_bytes=job.get("file_size", 0),
                created_at=job.get("created_at", get_timestamp()),
                vector_store_id=vector_store_id,
                status=job.get("status", "COMPLETED"),
                deduplicated=True,
            )

    # Vecchi job dello stesso file logico (nome): da rimuovere a ingest COMPLETED.
    supersedes = [
        j["file_id"] for j in existing
        if j.get("file_id") and j["file_id"] != file_data.file_id
    ]

    # 3) Crea job su Mongo
    now_ts = get_timestamp()
    job_doc = {
        "vector_store_id": vector_store_id,
        "file_id": file_data.file_id,
        "filename": filename,
        "file_size": file_metadata.get("bytes"),
        "content_hash": content_hash,
        "attributes": attributes,
        "supersedes_file_ids": supersedes,
        "file_path": file_path,
        "status": "PENDING",
        "error": None,
        "created_at": now_ts,
        "updated_at": now_ts,
        "stats": {
            "num_chunks": 0,
        },
    }

    result = await asyncio.to_thread(ingestion_jobs.insert_one, job_doc)

    job_id = str(result.inserted_id)

    return VectorStoreFile(
        job_id=job_id,
        id=file_data.file_id,  # che cazzo metti il vs ?=?????
        usage_bytes=file_metadata.get("bytes"),
        created_at=get_timestamp(),
        vector_store_id=vector_store_id,
        status="PENDING",
        # chunking_strategy=file_data.chunking_strategy
    )


@router.get("/{vector_store_id}/files/{file_id}")
async def get_vector_store_file(vector_store_id: str, file_id: str):
    """Download file content and chunks with paginate"""
    try:
        if not file_id.startswith("file-"):
            raise HTTPException(status_code=404, detail="File not found")

        file_path = os.path.join(FILES_STORAGE, file_id)
        metadata_path = os.path.join(FILES_STORAGE, f"{file_id}_metadata.json")

        if not os.path.exists(file_path) or not os.path.exists(metadata_path):
            raise HTTPException(status_code=404, detail="File not found")

        if not vector_store_id.startswith("vs_"):
            raise HTTPException(status_code=404, detail="Vector store not found")

        # Check if collection exists
        collections_response = await asyncio.to_thread(qdrant_client.get_collections)
        collections = [c.name for c in collections_response.collections]
        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        async with aiofiles.open(metadata_path, 'r') as f:
            file_metadata = json.loads(await f.read())

        job = await asyncio.to_thread(ingestion_jobs.find_one, {"file_id": file_id})
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # page = max(chunks_page, 1)
        # page_size = min(chunks_page_size, 100)
        # skip = (page - 1) * page_size

        # cursor = ingestion_job_chunks.find({"job_id": job["_id"]}).sort("chunk_index", 1).skip(skip).limit(page_size)

        # chunks = await asyncio.to_thread(cursor.to_list, page_size)

        # chunks = [
        #     {k: v for k, v in c.items() if k not in ("_id", "job_id")}
        #     for c in chunks
        # ]

        # Restituisci i dati
        return {
            "id": file_id,
            "vector_store_id": vector_store_id,
            "filename": file_metadata["filename"],
            "status": job["status"],
            "error": job["error"],
            "stats": job["stats"],
            "created_at": job["created_at"],
            "metadata": file_metadata,
            # "content": job["parser_doc_markdown"],
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error in get_vector_store_file: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to get file content: {str(e)}")


@router.get("/{vector_store_id}/files")
def list_vector_store_files(vector_store_id: str):
    """List files in vector store (based on ingestion_jobs, NOT scanning all Qdrant points)"""
    try:
        if not vector_store_id.startswith("vs_"):
            raise HTTPException(status_code=404, detail="Vector store not found")

        # (Opzionale) verifica che la collection esista in Qdrant
        collections = [c.name for c in qdrant_client.get_collections().collections]
        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        # Leggi tutti i job e tieni UN solo job per file_id (il più recente):
        # difensivo verso dati legacy in cui un file ha più job accumulati.
        jobs_cursor = ingestion_jobs.find({"vector_store_id": vector_store_id})
        latest_by_file: Dict[str, Any] = {}
        for job in jobs_cursor:
            file_id = job.get("file_id")
            if not file_id:
                continue
            prev = latest_by_file.get(file_id)
            if prev and prev.get("created_at", 0) >= job.get("created_at", 0):
                continue
            latest_by_file[file_id] = job

        files = []
        for file_id, job in latest_by_file.items():
            stats = job.get("stats", {}) or {}
            files.append({
                "id": f"{vector_store_id}_{file_id}",
                "usage_bytes": job.get("file_size", 0),
                "created_at": job.get("created_at", get_timestamp()),
                "vector_store_id": vector_store_id,
                "status": job.get("status", "unknown"),
                "file_id": file_id,
                "filename": job.get("filename", "unknown"),
                "num_chunks": stats.get("num_chunks", 0),
                # attributes custom (slug, ecc.) per la UI
                "attributes": job.get("attributes", {}) or {},
                # provenienza: se valorizzato il file viene da una sync SharePoint
                # (collega il file al sync job per conteggio/raggruppamento UI)
                "sharepoint_job_id": (
                    str(job["sharepoint_job_id"]) if job.get("sharepoint_job_id") else None
                ),
                # "num_pages": stats.get("num_pages", 0),
            })

        # Ordina per created_at desc
        files.sort(key=lambda f: f["created_at"], reverse=True)

        return {
            "object": "list",
            "data": files,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error in list_vector_store_files: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to list vector store files: {str(e)}")
