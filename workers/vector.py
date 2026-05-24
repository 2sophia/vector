"""
Vector ingestion worker.

Polling loop: pesca job PENDING da MongoDB, chiama Docling per il chunking,
genera embeddings con BGE-M3 e scrive i punti su Qdrant.

Garantisce:
- claim atomico PENDING → PROCESSING (concurrent-safe se più worker)
- idempotenza: prima di indicizzare un file_id, cancella i punti Qdrant esistenti
- cleanup orfani: in caso di failure a metà upload, rimuove i punti parziali
"""

import uuid
import asyncio
import logging
from typing import List, Dict, Any

from qdrant_client.models import PointStruct

from utils import get_timestamp
from utils.database import db
from utils.qdrant import qdrant_client, delete_qdrant_points
from utils.docling import upload_file_for_chunking_sync
from utils.embeddings import get_bge_embeddings
from utils.filesystem import delete_file_from_disk
from utils.falkor import write_document_graph, purge_file_graph
from utils.entities import extract_entities_batch, warmup as entities_warmup

# ---------------------------------------------------------------------------
# Config (centralizzata in utils/config, prefisso SOPHIA_VECTOR_)
# ---------------------------------------------------------------------------

from utils.settings import (
    INGEST_BATCH_SIZE,
    INGEST_MAX_CONCURRENT_JOBS as MAX_CONCURRENT_JOBS,
    INGEST_WAIT_TIME_JOBS as POLL_INTERVAL,
)

jobs_coll = db["ingestion_jobs"]

# Campi di sistema del payload Qdrant: gli attributes custom NON possono
# sovrascriverli. Tutto il resto degli attributes viene appiattito top-level
# (contratto prod / engine old: lo slug `sophia_directory_slug`,
# `sharepoint_file_id`, ecc. sono filtrati top-level dal consumer agent).
RESERVED_PAYLOAD_KEYS = frozenset({
    "job_id", "file_id", "vector_store_id", "filename",
    "chunk_index", "text", "headings", "page_numbers",
})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-worker")


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

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


async def set_job_status(job_id, status: str, extra: Dict[str, Any] | None = None):
    """Aggiorna lo stato di un job su MongoDB."""
    update = {"status": status, "updated_at": get_timestamp()}
    if extra:
        update.update(extra)

    def _update():
        jobs_coll.update_one({"_id": job_id}, {"$set": update})

    await asyncio.to_thread(_update)


# ---------------------------------------------------------------------------
# Qdrant helpers
# ---------------------------------------------------------------------------

async def purge_file_points(collection_name: str, file_id: str) -> None:
    """
    Rimuove tutti i punti Qdrant relativi a un file_id.
    Best-effort: logga un warning ma non solleva, così un cleanup mancato
    non blocca l'ingest.
    """
    try:
        await asyncio.to_thread(delete_qdrant_points, collection_name, "file_id", file_id)
    except Exception as e:
        logger.warning(f"purge_file_points({collection_name}, {file_id}) failed: {e}")


async def cleanup_superseded(job: Dict[str, Any], collection_name: str) -> None:
    """Re-ingest sicuro: rimuove i vecchi file sostituiti (stesso file logico)
    SOLO dopo che il nuovo ingest è andato a buon fine — punti Qdrant + job
    Mongo + file su disco. Se l'ingest fosse fallito, i vecchi sarebbero rimasti
    intatti e il documento resterebbe ricercabile col contenuto precedente.
    """
    for old_id in (job.get("supersedes_file_ids") or []):
        if not old_id:
            continue
        await purge_file_points(collection_name, old_id)
        # rimuovi anche i nodi grafo del vecchio file (best-effort)
        await asyncio.to_thread(purge_file_graph, collection_name, old_id)
        await asyncio.to_thread(
            jobs_coll.delete_many,
            {"vector_store_id": collection_name, "file_id": old_id},
        )
        try:
            await delete_file_from_disk(old_id)
        except Exception as e:
            logger.warning(f"cleanup_superseded: delete_file_from_disk({old_id}) failed: {e}")
        logger.info(f"♻️ superseded {old_id} rimosso dopo re-ingest")


# ---------------------------------------------------------------------------
# Pipeline per singolo job
# ---------------------------------------------------------------------------

async def handle_job(job: Dict[str, Any]):
    job_id = job["_id"]
    tag = str(job_id)
    file_id = job["file_id"]
    file_path = job["file_path"]
    vector_store_id = job["vector_store_id"]

    logger.info(f"[{tag}] Start ingestion file_id={file_id} path={file_path}")

    # Idempotenza: rimuovi eventuali punti pre-esistenti per questo file
    await purge_file_points(vector_store_id, file_id)

    # 1) Chunking via Docling
    try:
        result = await asyncio.to_thread(upload_file_for_chunking_sync, file_path)
    except Exception as e:
        logger.exception(f"[{tag}] Docling chunking failed: {e}")
        await set_job_status(job_id, "FAILED", {"error": f"Chunking error: {e}"})
        return

    chunks = result.get("chunks") or []
    if not chunks:
        logger.info(f"[{tag}] No chunks produced, COMPLETED (empty)")
        # nessun chunk → assicura che non restino nodi grafo del file (re-ingest svuotato)
        await asyncio.to_thread(purge_file_graph, vector_store_id, file_id)
        await cleanup_superseded(job, vector_store_id)
        await set_job_status(job_id, "COMPLETED", {"stats.num_chunks": 0})
        return

    logger.info(f"[{tag}] Docling produced {len(chunks)} chunks")

    # 2) Embeddings + upsert Qdrant a batch
    total_indexed = 0
    attributes = job.get("attributes") or {}
    # Accumula i chunk (con il point_id Qdrant) per scrivere il grafo a fine job.
    graph_chunks: List[Dict[str, Any]] = []

    for batch_start in range(0, len(chunks), INGEST_BATCH_SIZE):
        batch = chunks[batch_start: batch_start + INGEST_BATCH_SIZE]
        batch_num = batch_start // INGEST_BATCH_SIZE + 1

        # Prepara doc + testi (scarta chunk vuoti)
        docs = []
        texts = []
        for chunk in batch:
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            docs.append({
                "file_id": file_id,
                "vector_store_id": vector_store_id,
                "filename": job["filename"],
                "chunk_index": chunk.get("chunk_index"),
                "text": text,
                "headings": chunk.get("headings") or [],
                "page_numbers": chunk.get("page_numbers") or [],
            })
            texts.append(text)

        if not docs:
            continue

        # Embeddings
        try:
            dense_vecs, sparse_vecs = await asyncio.to_thread(get_bge_embeddings, texts)
        except Exception as e:
            logger.exception(f"[{tag}] Embedding failed at batch {batch_num}: {e}")
            await purge_file_points(vector_store_id, file_id)
            await set_job_status(job_id, "FAILED", {"error": f"Embedding error: {e}"})
            return

        # Entity extraction (M3, best-effort: un guasto qui non fa fallire il job)
        try:
            chunk_entities = await asyncio.to_thread(extract_entities_batch, texts)
        except Exception as e:
            logger.warning(f"[{tag}] entity extraction failed at batch {batch_num}: {e}")
            chunk_entities = [[] for _ in texts]

        # Costruisci PointStruct.
        # Gli attributes vengono APPIATTITI top-level (contratto prod): es.
        # `sophia_directory_slug`/`sharepoint_file_id` sono filtrati top-level
        # dall'agent consumer. I campi di sistema (RESERVED_PAYLOAD_KEYS) hanno
        # la precedenza: un attribute custom non può sovrascriverli.
        points = []
        for i, (doc, dense, sparse) in enumerate(zip(docs, dense_vecs, sparse_vecs)):
            payload = {
                "job_id": tag,
                "file_id": doc["file_id"],
                "vector_store_id": doc["vector_store_id"],
                "filename": doc["filename"],
                "chunk_index": doc["chunk_index"],
                "text": doc["text"],
                "headings": doc["headings"],
                "page_numbers": doc["page_numbers"],
            }
            for k, v in attributes.items():
                if k not in RESERVED_PAYLOAD_KEYS:
                    payload[k] = v
            point_id = str(uuid.uuid4())
            points.append(PointStruct(
                id=point_id,
                vector={"dense": dense, "sparse": sparse},
                payload=payload,
            ))
            # stesso point_id sul nodo :Chunk → ponte grafo ↔ Qdrant
            graph_chunks.append({
                "chunk_index": doc["chunk_index"],
                "headings": doc["headings"],
                "page_numbers": doc["page_numbers"],
                "text": doc["text"],
                "qdrant_point_id": point_id,
                "entities": chunk_entities[i] if i < len(chunk_entities) else [],
            })

        # Upsert Qdrant
        try:
            await asyncio.to_thread(
                qdrant_client.upload_points,
                vector_store_id,
                points,
                True,
            )
        except Exception as e:
            logger.exception(f"[{tag}] Qdrant upsert failed at batch {batch_num}: {e}")
            await purge_file_points(vector_store_id, file_id)
            await set_job_status(job_id, "FAILED", {"error": f"Qdrant upload error: {e}"})
            return

        total_indexed += len(docs)
        logger.info(f"[{tag}] Batch {batch_num}: {len(docs)} points indexed ({total_indexed}/{len(chunks)})")

    # 3) Grafo strutturale (best-effort, additivo: un guasto qui NON fa fallire
    # il job, i punti Qdrant sono già scritti). Scrive doc→sezioni→chunk con il
    # qdrant_point_id come ponte.
    slug = attributes.get("sophia_directory_slug")
    await asyncio.to_thread(
        write_document_graph,
        vector_store_id,
        file_id,
        job["filename"],
        slug,
        job.get("content_hash"),
        get_timestamp(),
        graph_chunks,
    )

    # 4) Completato — ora che i nuovi chunk sono indicizzati, rimuovi i vecchi
    # file sostituiti (re-ingest sicuro: solo dopo il successo).
    await cleanup_superseded(job, vector_store_id)
    await set_job_status(job_id, "COMPLETED", {"stats.num_chunks": total_indexed})
    logger.info(f"[{tag}] COMPLETED — {total_indexed} chunks indexed")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main_loop():
    logger.info(
        f"🧵 Vector worker started | "
        f"batch_size={INGEST_BATCH_SIZE} concurrent_jobs={MAX_CONCURRENT_JOBS} poll={POLL_INTERVAL}s"
    )

    # Pre-carica GLiNER (se abilitato) fuori dall'event loop: il log del device
    # (CPU) compare subito all'avvio del worker, e il primo job non paga il load.
    await asyncio.to_thread(entities_warmup)

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
