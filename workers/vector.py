"""
Vector ingestion worker.

Polling loop: pesca job PENDING da MongoDB, chiama Docling per il chunking,
genera embeddings con BGE-M3 e scrive i punti su Qdrant.

Garantisce:
- claim atomico PENDING → PROCESSING (concurrent-safe se più worker)
- idempotenza: prima di indicizzare un file_id, cancella i punti Qdrant esistenti
- cleanup orfani: in caso di failure a metà upload, rimuove i punti parziali
"""

import os
import uuid
import shutil
import asyncio
import logging
import threading
from typing import List, Dict, Any

from qdrant_client.models import PointStruct

from utils import get_timestamp
from utils.database import db
from utils.qdrant import qdrant_client, delete_qdrant_points
from utils.docling import upload_file_for_chunking_sync, clear_caches
from utils.convert import normalize_for_parser, UnsupportedFormatError
from utils.tabular import is_tabular, chunk_tabular
from utils.exclusions import is_excluded
from utils.embeddings import get_bge_embeddings
from utils.filesystem import delete_file_from_disk
from utils.falkor import write_document_graph, purge_file_graph, write_relations
from models.ner import NerModel
from utils import model_client
from utils.settings import GLINER_ENABLED, CLASSIFIER_ENABLED, TABULAR_ENABLED

# Estrazione entità: i modelli PESANTI (GLiNER/relex/classifier) e Whisper vivono nel
# BACKEND e si chiamano via model_client (HTTP) → una sola copia, nessun peso qui.
from utils.curation import body_hash, register_document_bodies, purge_file_bodies
from utils.store_schema import get_effective_schema
from utils.settings import CURATION_ENABLED

# ---------------------------------------------------------------------------
# Config (centralizzata in utils/config, prefisso SOPHIA_VECTOR_)
# ---------------------------------------------------------------------------

from utils.settings import (
    INGEST_BATCH_SIZE,
    INGEST_MAX_CONCURRENT_JOBS as MAX_CONCURRENT_JOBS,
    INGEST_WAIT_TIME_JOBS as POLL_INTERVAL,
    DOCLING_CLEAR_EVERY,
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
# Anti-leak Docling (Layer 1): l'OCR trattiene i modelli → la RAM del parser
# sale e non scende. Svuotiamo le cache ogni N doc completati, e comunque a fine
# batch. Fire-and-forget in un thread daemon: best-effort, non blocca né rallenta
# l'ingestion (il clear NON libera la VRAM, che richiede il restart del container).
# ---------------------------------------------------------------------------
_docs_since_clear = 0


def _fire_docling_clear() -> None:
    threading.Thread(target=clear_caches, daemon=True, name="docling-clear").start()
    logger.info("🧹 Docling cache clear (fire-and-forget)")


def _maybe_clear_docling(force: bool = False) -> None:
    """Conta i doc completati; lancia il clear ogni DOCLING_CLEAR_EVERY, e a fine
    batch (force=True) se c'è stato almeno un doc dall'ultimo clear. 0 = off."""
    global _docs_since_clear
    if DOCLING_CLEAR_EVERY <= 0:
        return
    if force:
        if _docs_since_clear > 0:
            _docs_since_clear = 0
            _fire_docling_clear()
        return
    _docs_since_clear += 1
    if _docs_since_clear >= DOCLING_CLEAR_EVERY:
        _docs_since_clear = 0
        _fire_docling_clear()


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


# Reaper: un job PROCESSING che non avanza da oltre questo intervallo è orfano
# (worker crashato/killato a metà → resterebbe PROCESSING per sempre, mai
# ri-claimato perché claim cerca solo PENDING). Soglia ALTA di proposito: un job
# legittimo può durare a lungo (Docling fino a ~30 min sui PDF tabellari, ASR su
# video) → meglio aspettare che rubare un job ancora vivo. updated_at è settato al
# claim e a ogni cambio stato.
STALE_PROCESSING_SECONDS = 3600


async def reap_stale_processing() -> None:
    """Rimette PENDING i job rimasti orfani in PROCESSING oltre la soglia, così
    vengono ri-elaborati. Best-effort: un errore qui non deve fermare il worker."""
    cutoff = get_timestamp() - STALE_PROCESSING_SECONDS

    def _reap():
        return jobs_coll.update_many(
            {"status": "PROCESSING", "updated_at": {"$lt": cutoff}},
            {"$set": {"status": "PENDING", "updated_at": get_timestamp()}},
        )

    try:
        res = await asyncio.to_thread(_reap)
        if getattr(res, "modified_count", 0):
            logger.warning(f"♻️ reaper: {res.modified_count} job orfani in PROCESSING rimessi PENDING")
    except Exception as e:
        logger.warning(f"reaper job PROCESSING orfani fallito: {e}")


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
        if CURATION_ENABLED:
            await asyncio.to_thread(purge_file_bodies, collection_name, old_id)
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
    """Safety-net attorno a _process_job: qualunque eccezione NON prevista forza il
    job a FAILED, così non resta MAI orfano in PROCESSING. Gli errori attesi (file
    rotto, conversione, chunking, embedding, upsert) sono già gestiti a valle."""
    job_id = job.get("_id")
    try:
        await _process_job(job)
    except Exception as e:
        logger.exception(f"[{job_id}] errore non gestito → FAILED: {e}")
        try:
            await set_job_status(job_id, "FAILED", {"error": f"unhandled: {e}"})
        except Exception:
            logger.exception(f"[{job_id}] impossibile marcare FAILED dopo errore non gestito")


async def _process_job(job: Dict[str, Any]):
    job_id = job["_id"]
    tag = str(job_id)
    file_id = job["file_id"]
    file_path = job["file_path"]
    vector_store_id = job["vector_store_id"]

    logger.info(f"[{tag}] Start ingestion file_id={file_id} path={file_path}")

    # Guard esclusione: il file è stato marcato EXCLUDED dopo la creazione del job
    # → non lo processiamo. Difensivo (di norma un file escluso ha già i job a
    # EXCLUDED e non viene ri-pescato): copre la corsa creazione-job → esclusione.
    # Identità: file_id manuale + sharepoint_file_id (id Graph, stabile tra le sync).
    sp_file_id = (job.get("attributes") or {}).get("sharepoint_file_id")
    if await asyncio.to_thread(
        is_excluded, vector_store_id, file_id=file_id, sharepoint_file_id=sp_file_id
    ):
        logger.info(f"[{tag}] file marcato EXCLUDED → skip")
        await set_job_status(job_id, "EXCLUDED", {"error": None})
        return

    # Guard file rotto: mancante o 0 byte → FAILED subito, niente parse inutile.
    # Casi reali visti in prod: download SharePoint fallito che lascia un file vuoto,
    # o PDF corrotto a 0 byte → Docling darebbe PdfiumError e 0 chunk "COMPLETED"
    # silenziosi (il problema resta invisibile). Meglio un FAILED esplicito.
    try:
        size = os.path.getsize(file_path)
    except OSError:
        size = -1
    if size <= 0:
        reason = "file mancante su disco" if size < 0 else "file vuoto (0 byte)"
        logger.warning(f"[{tag}] {reason} → FAILED")
        await set_job_status(job_id, "FAILED", {"error": reason})
        return

    # Idempotenza: rimuovi eventuali punti pre-esistenti per questo file
    await purge_file_points(vector_store_id, file_id)
    # ...e le entry di provenienza curation (conteggi boilerplate corretti su re-ingest)
    if CURATION_ENABLED:
        await asyncio.to_thread(purge_file_bodies, vector_store_id, file_id)

    # Schema di INGESTION effettivo, risolto UNA volta a cascata
    # file→directory→sync→store→default globale (utils.store_schema): governa il chunk
    # size (usato ORA, prima del chunking) e le label di estrazione (usate dopo).
    attributes = job.get("attributes") or {}
    directory_slug = attributes.get("sophia_directory_slug")
    # sharepoint_job_id è un ObjectId nei job: str() per combaciare con lo scope "sync".
    sync_id = str(job["sharepoint_job_id"]) if job.get("sharepoint_job_id") else None
    schema = await asyncio.to_thread(
        get_effective_schema, vector_store_id, directory_slug, sync_id, file_id
    )

    # 0) Normalizza per il parser: i formati che Docling non mangia (mail,
    # Office binario, rtf, odf, txt) vengono convertiti in una tempdir prima del
    # chunking. I formati nativi passano invariati (tmp_dir=None).
    try:
        parse_path, tmp_dir = await asyncio.to_thread(normalize_for_parser, file_path)
    except UnsupportedFormatError as e:
        logger.warning(f"[{tag}] Unsupported format {e}, FAILED")
        await set_job_status(job_id, "FAILED", {"error": f"Unsupported format: {e}"})
        return
    except Exception as e:
        logger.exception(f"[{tag}] Pre-parser conversion failed: {e}")
        await set_job_status(job_id, "FAILED", {"error": f"Conversion error: {e}"})
        return

    # 1) Chunking. Le tabelle (csv/xlsx) NON passano da Docling: una tabella grande
    # esplode (0 chunk). Chunker tabulare dedicato → table card + righe verbalizzate
    # (vedi utils/tabular.py). Tutto il resto va a Docling. chunk_max_tokens viene
    # dallo schema effettivo (override per file/dir/store). Tempdir ripulita a fine parse.
    try:
        if TABULAR_ENABLED and is_tabular(parse_path):
            result = await asyncio.to_thread(chunk_tabular, parse_path)
        else:
            result = await asyncio.to_thread(
                upload_file_for_chunking_sync, parse_path, schema["chunk_max_tokens"]
            )
    except Exception as e:
        logger.exception(f"[{tag}] Chunking failed: {e}")
        await set_job_status(job_id, "FAILED", {"error": f"Chunking error: {e}"})
        return
    finally:
        if tmp_dir:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)

    chunks = result.get("chunks") or []
    if not chunks:
        # File valido ma nessun contenuto estraibile (scansione vuota, PDF corrotto
        # non a 0 byte, doc senza testo). → FAILED, non COMPLETED: un COMPLETED muto
        # direbbe "indicizzato" mentre il sistema non ha tirato fuori NULLA — l'utente
        # deve saperlo e poter agire. NON chiamo cleanup_superseded: se esisteva una
        # versione precedente buona resta indicizzata (non la cancello perché il
        # re-parse ha prodotto zero). I punti/grafo di QUESTO file sono già stati
        # ripuliti a inizio job (idempotenza) → niente residui.
        logger.warning(f"[{tag}] 0 chunk estratti → FAILED (nessun contenuto estraibile)")
        await asyncio.to_thread(purge_file_graph, vector_store_id, file_id)
        await set_job_status(job_id, "FAILED", {"error": "nessun contenuto estraibile dal file"})
        return

    logger.info(f"[{tag}] Docling produced {len(chunks)} chunks")

    # 2) Embeddings + upsert Qdrant a batch
    total_indexed = 0
    # Accumula i chunk (con il point_id Qdrant) per scrivere il grafo a fine job.
    graph_chunks: List[Dict[str, Any]] = []
    # Body-hash distinti del documento (curation): un disclaimer ripetuto N volte
    # nello stesso file conta una volta sola → set, registrato a fine job.
    doc_body_hashes: set = set()
    # Relazioni tipizzate del documento (M5, GLiNER-relex): accumulate e scritte a
    # fine job (write_relations aggrega/dedup per (head,type,tail)).
    doc_relations: List[Dict[str, Any]] = []
    # Label di estrazione dallo schema effettivo (già risolto prima del chunking).
    entity_labels = schema["entity_labels"]
    relation_labels = schema["relation_labels"]
    relations_on = schema["relations_enabled"]

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

        # Entity extraction (M3, best-effort). GLINER off → SOLO regex in-process (nessun
        # modello, nessuna chiamata). GLINER on → il backend fa regex+GLiNER via HTTP (una
        # fonte sola, niente regex duplicate). Un guasto qui non fa fallire il job.
        try:
            if GLINER_ENABLED:
                chunk_entities = await asyncio.to_thread(model_client.ner, texts, entity_labels)
            else:
                chunk_entities = await asyncio.to_thread(NerModel.regex_only, texts)
        except Exception as e:
            logger.warning(f"[{tag}] entity extraction failed at batch {batch_num}: {e}")
            chunk_entities = [[] for _ in texts]

        # Relation extraction (M5, opt-in dallo schema effettivo, best-effort) → backend
        if relations_on:
            try:
                rel_batch = await asyncio.to_thread(
                    model_client.relex, texts, entity_labels, relation_labels
                )
                for rels in rel_batch:
                    doc_relations.extend(rels)
            except Exception as e:
                logger.warning(f"[{tag}] relation extraction failed at batch {batch_num}: {e}")

        # Classificazione (GliClass zero-shot, opt-in): tag tema/tipo/sensibilità sul
        # chunk → payload per faceting/filtri nella search. Best-effort → backend.
        chunk_categories = [[] for _ in texts]
        if CLASSIFIER_ENABLED:
            try:
                cls_batch = await asyncio.to_thread(model_client.classify, texts)
                chunk_categories = [[r["label"] for r in rows] for rows in cls_batch]
            except Exception as e:
                logger.warning(f"[{tag}] classification failed at batch {batch_num}: {e}")

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
            # tag di classificazione (se il classifier è attivo) → faceting search
            cats = chunk_categories[i] if i < len(chunk_categories) else []
            if cats:
                payload["category"] = cats
            point_id = str(uuid.uuid4())
            points.append(PointStruct(
                id=point_id,
                vector={"dense": dense, "sparse": sparse},
                payload=payload,
            ))
            # body-hash del chunk (testo senza prefisso heading): identifica lo
            # stesso contenuto tra documenti/sezioni diversi → segnale boilerplate.
            bh = body_hash(doc["text"], doc["headings"]) if CURATION_ENABLED else None
            if bh:
                doc_body_hashes.add(bh)
            # stesso point_id sul nodo :Chunk → ponte grafo ↔ Qdrant
            graph_chunks.append({
                "chunk_index": doc["chunk_index"],
                "headings": doc["headings"],
                "page_numbers": doc["page_numbers"],
                "text": doc["text"],
                "qdrant_point_id": point_id,
                "body_hash": bh,
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

    # 3) Layer additivi (grafo strutturale, provenienza curation, relazioni
    # tipizzate). best-effort REALE: i punti Qdrant sono GIÀ scritti → il documento
    # è ricercabile, un guasto qui NON deve far fallire il job. Avvolti insieme:
    # prima questo blocco non era protetto e un'eccezione (FalkorDB giù, ecc.)
    # lasciava il job orfano in PROCESSING nonostante l'indicizzazione riuscita.
    try:
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

        # 3b) Provenienza curation: registra in quali documenti compare ogni body
        # (fonte di verità per la soppressione boilerplate a search-time).
        if CURATION_ENABLED and doc_body_hashes:
            await asyncio.to_thread(
                register_document_bodies, vector_store_id, file_id, list(doc_body_hashes)
            )

        # 3c) Relazioni tipizzate (M5): archi :REL tra entità.
        if relations_on and doc_relations:
            await asyncio.to_thread(write_relations, vector_store_id, file_id, doc_relations)
    except Exception as e:
        logger.warning(f"[{tag}] layer additivi (grafo/curation/relazioni) falliti, proseguo verso COMPLETED: {e}")

    # 4) Completato — ora che i nuovi chunk sono indicizzati, rimuovi i vecchi
    # file sostituiti (re-ingest sicuro: solo dopo il successo).
    await cleanup_superseded(job, vector_store_id)
    await set_job_status(job_id, "COMPLETED", {"stats.num_chunks": total_indexed})
    logger.info(f"[{tag}] COMPLETED — {total_indexed} chunks indexed")
    _maybe_clear_docling()  # Layer 1 anti-leak: clear ogni N doc


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main_loop():
    logger.info(
        f"🧵 Vector worker started | "
        f"batch_size={INGEST_BATCH_SIZE} concurrent_jobs={MAX_CONCURRENT_JOBS} poll={POLL_INTERVAL}s"
    )

    # Pre-carica i modelli (se abilitati) fuori dall'event loop: i device finiscono
    # subito nei log all'avvio del worker, e il primo job non paga il load.
    # I modelli pesanti (GLiNER/relex/classifier) vivono nel backend: il worker non fa più
    # warmup né li carica — li chiama via model_client (HTTP). Le regex (in-process) sono
    # già pronte. Niente VRAM/RAM per i modelli in questo processo.

    # Recupera all'avvio i job rimasti orfani in PROCESSING da un worker morto.
    await reap_stale_processing()

    while True:
        try:
            jobs = await claim_pending_jobs(MAX_CONCURRENT_JOBS)

            if jobs:
                logger.info(f"🔎 Claimed {len(jobs)} job(s), processing...")
                tasks = [asyncio.create_task(handle_job(job)) for job in jobs]
                # return_exceptions=True: un task che esplodesse (non dovrebbe — il
                # safety-net di handle_job cattura tutto) NON aborta gli altri.
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.error(f"⚠️ task handle_job ha sollevato (inatteso): {r}")
                continue  # subito al prossimo poll se c'è ancora coda

        except Exception as e:
            logger.exception(f"⚠️ Unhandled error in main loop: {e}")
            await asyncio.sleep(5.0)
            continue

        # Coda vuota → recupero orfani + clear finale di fine batch, poi poll lento
        await reap_stale_processing()
        _maybe_clear_docling(force=True)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
