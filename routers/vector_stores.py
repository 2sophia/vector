"""Vector Store endpoints"""
import asyncio
import os
import json

from typing import Dict, Any, Optional

import aiofiles
import aiofiles.os

from fastapi import HTTPException, Query, APIRouter, Body, Header, BackgroundTasks

from utils.database import db
from utils.docling import PARSER_SUPPORTED_EXTENSIONS
from utils.filesystem import get_file_metadata, get_file_path, delete_file_from_disk, is_valid_file_id, write_json_atomic, _find_file_path
from utils.qdrant import (
    create_qdrant_collection, delete_qdrant_points,
    find_redundant_clusters, mark_redundant, unmark_redundant,
    find_semantic_outliers, semantic_clusters, count_points_by_slug,
)
from utils.falkor import (
    delete_graph, purge_file_graph, export_graph, optimize_graph, graph_stats,
    find_relation_conflicts,
)
from utils.curation import purge_file_bodies, delete_collection_bodies, curation_stats
from utils.exclusions import exclude_file, unexclude_file, list_excluded, is_excluded
from utils.store_schema import (
    get_effective_schema, get_schema_doc, set_schema, delete_store_schemas,
)

# Import DA UTILS (non serve il punto perché siamo fuori dal pacchetto)
from utils import (
    get_logger,

    qdrant_client,

    get_timestamp,
    generate_id
)

from utils.schemas import (
    VectorStoreCreate,
    VectorStoreUpdate,
    FileAttach,
    VectorStore,
    VectorStoreFile,
    StoreSchemaUpdate,
)

from utils.settings import (
    FILES_STORAGE,
    CURATION_BOILERPLATE_RATIO,
    CURATION_BOILERPLATE_MIN_DOCS,
)

logger = get_logger(__name__)

# create collections for jobs async management
ingestion_jobs = db["ingestion_jobs"]
directories_coll = db["directories"]
overview_cache = db["vector_overview_cache"]
optimize_jobs = db["optimize_jobs"]
# L'optimize completo (near-duplicate O(N) round-trip Qdrant + graph scan) può durare
# minuti su collection grandi → va in HTTP timeout dietro il proxy. Gira quindi come
# JOB ASINCRONO: POST crea il record e lancia il lavoro in background, il client fa
# polling sul GET. Stato in Mongo = robusto al multi-worker uvicorn. Un job "running"
# più vecchio di questa soglia è considerato orfano (worker morto a metà) → failed.
STALE_OPTIMIZE_SECONDS = 1800
# ingestion_job_chunks = db["ingestion_job_chunks"]

# Crea il router (come una mini-app)
router = APIRouter(
    prefix="/v1/vector_stores",  # Tutti gli endpoint iniziano con questo
    tags=["Vector Stores"]  # Per la documentazione
)


# ================== VECTOR STORE ENDPOINTS ==================

# TODO: WORK
def _file_counts(vector_store_id: str) -> Dict[str, int]:
    """Conta i FILE distinti (per file_id) per status dai job di ingestion — NON i
    chunk/punti Qdrant (che sono molti per file). Status job: PENDING/PROCESSING/
    COMPLETED/FAILED."""
    jobs = db["ingestion_jobs"]
    base = {"vector_store_id": vector_store_id}

    def n(extra: Dict[str, Any]) -> int:
        return len(jobs.distinct("file_id", {**base, **extra}))

    return {
        "total": n({}),
        "completed": n({"status": "COMPLETED"}),
        "in_progress": n({"status": {"$in": ["PENDING", "PROCESSING"]}}),
        "failed": n({"status": "FAILED"}),
        "excluded": n({"status": "EXCLUDED"}),
        "cancelled": 0,
    }


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
        await write_json_atomic(metadata_path, collection_info)

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
                file_counts=await asyncio.to_thread(_file_counts, collection_name),
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
            file_counts=await asyncio.to_thread(_file_counts, vector_store_id),
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


@router.patch("/{vector_store_id}", response_model=VectorStore)
async def update_vector_store(vector_store_id: str, body: VectorStoreUpdate):
    """Aggiorna i metadati di un vector store (rinomina / metadata). Il nome vive nel
    sidecar JSON `{id}_metadata.json` (Qdrant non tiene i metadati di collection)."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    collections = [c.name for c in (await asyncio.to_thread(qdrant_client.get_collections)).collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")

    metadata_path = os.path.join(FILES_STORAGE, f"{vector_store_id}_metadata.json")
    try:
        async with aiofiles.open(metadata_path, "r") as f:
            meta = json.loads(await f.read())
    except Exception:
        meta = {"name": vector_store_id, "created_at": get_timestamp(), "metadata": {}}

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name cannot be empty")
        meta["name"] = name
    if body.metadata is not None:
        meta["metadata"] = body.metadata

    await write_json_atomic(metadata_path, meta)

    point_count = 0
    try:
        info = await asyncio.to_thread(qdrant_client.get_collection, vector_store_id)
        point_count = info.points_count or 0
    except Exception:
        pass
    return VectorStore(
        id=vector_store_id,
        name=meta.get("name", vector_store_id),
        status="completed",
        usage_bytes=point_count * 1024,
        created_at=meta.get("created_at", get_timestamp()),
        file_counts=await asyncio.to_thread(_file_counts, vector_store_id),
        metadata=meta.get("metadata", {}),
        last_active_at=get_timestamp(),
    )


@router.get("/{vector_store_id}/curation")
def get_curation_stats(vector_store_id: str):
    """Metriche di data curation della collection — "quanto boilerplate hai".

    Racconta la tesi del prodotto in un numero: documenti totali, contenuti (body)
    distinti, quanti sono boilerplate (stesso testo in almeno MIN_DOCS documenti e
    in oltre RATIO della collection) e la frequenza del più diffuso. Utile anche
    per tarare le soglie sui dati reali.
    """
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    collections = [c.name for c in qdrant_client.get_collections().collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")
    stats = curation_stats(
        vector_store_id, CURATION_BOILERPLATE_RATIO, CURATION_BOILERPLATE_MIN_DOCS
    )
    return {
        "object": "vector_store.curation",
        "vector_store_id": vector_store_id,
        "boilerplate_ratio": CURATION_BOILERPLATE_RATIO,
        "boilerplate_min_docs": CURATION_BOILERPLATE_MIN_DOCS,
        **stats,
    }


def _run_optimize(vector_store_id: str, p: dict) -> dict:
    """Calcolo dell'ottimizzazione (sincrono, può durare minuti). Eseguito in
    background da un job — NON valida il vector store (già fatto alla POST). `p` =
    parametri (vedi optimize_vector_store). Semantica invariata, solo off-request.

    Ripulisce il knowledge graph su ciò che è già estratto: menzioni con score <
    `min_score`, entità "spazzatura" (nome corto/numerico) e orfane. Filtri agnostici.
    Riporta anche le metriche di data curation. `dry_run` non cancella nulla."""
    dry_run = p["dry_run"]
    reset_redundancy = p["reset_redundancy"]
    apply_redundancy = p["apply_redundancy"]
    dense_threshold = p["dense_threshold"]
    # Il graph-cleanup è distruttivo (irreversibile senza re-ingest): deve girare
    # SOLO su un Applica vero. Reset marcatura è un puro undo sui ridondanti →
    # forziamo il dry_run sul grafo così non cancella nulla.
    graph = optimize_graph(
        vector_store_id, min_score=p["min_score"], min_entity_len=p["min_entity_len"],
        drop_numeric=p["drop_numeric"], dry_run=dry_run or reset_redundancy,
    )
    curation = curation_stats(
        vector_store_id, CURATION_BOILERPLATE_RATIO, CURATION_BOILERPLATE_MIN_DOCS
    )
    # Ridondanza semantica (near-duplicate dense∩sparse).
    #  - dry_run o nessuna azione → solo detection (numeri)
    #  - apply_redundancy → marca i ridondanti (soppressi a search-time, reversibile)
    #  - reset_redundancy → rimuove la marcatura
    redundancy = None
    if p["include_redundancy"]:
        if not dry_run and reset_redundancy:
            unmark_redundant(vector_store_id)
            redundancy = find_redundant_clusters(vector_store_id, dense_threshold=dense_threshold)
            redundancy["reset"] = True
        elif not dry_run and apply_redundancy:
            redundancy = mark_redundant(vector_store_id, dense_threshold=dense_threshold)
        else:
            redundancy = find_redundant_clusters(vector_store_id, dense_threshold=dense_threshold)
    # Diagnostiche additive (sola lettura): off di default per non appesantire l'optimize.
    outliers = (
        find_semantic_outliers(vector_store_id, sim_threshold=p["outlier_sim_threshold"])
        if p["include_outliers"] else None
    )
    conflicts = find_relation_conflicts(vector_store_id) if p["include_conflicts"] else None
    if not dry_run:
        # graph cleanup / dedup non muovono points_count → invalida l'overview cachato
        overview_cache.delete_one({"_id": vector_store_id})
    return {
        "object": "vector_store.optimize",
        "vector_store_id": vector_store_id,
        "dry_run": dry_run,
        "graph": graph,
        "curation": curation,
        "redundancy": redundancy,
        "outliers": outliers,
        "conflicts": conflicts,
    }


def _execute_optimize_job(job_id: str, vector_store_id: str, p: dict) -> None:
    """Esegue _run_optimize e persiste esito/risultato su Mongo. Gira nel threadpool
    (BackgroundTasks) DOPO la risposta HTTP. Best-effort: un'eccezione → failed, mai
    lascia il job orfano in running (lo copre comunque il reaper del GET)."""
    try:
        result = _run_optimize(vector_store_id, p)
        optimize_jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "completed", "result": result,
                      "finished_at": get_timestamp(), "updated_at": get_timestamp()}},
        )
        logger.info(f"optimize job {job_id} completed ({vector_store_id})")
    except Exception as e:
        logger.exception(f"optimize job {job_id} failed: {e}")
        optimize_jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "failed", "error": str(e),
                      "finished_at": get_timestamp(), "updated_at": get_timestamp()}},
        )


@router.post("/{vector_store_id}/optimize")
def optimize_vector_store(
    vector_store_id: str,
    background_tasks: BackgroundTasks,
    min_score: float = Query(default=0.6, ge=0.0, le=1.0),
    min_entity_len: int = Query(default=3, ge=1),
    drop_numeric: bool = Query(default=True),
    dense_threshold: float = Query(default=0.96, ge=0.5, le=1.0),
    include_redundancy: bool = Query(default=True),
    apply_redundancy: bool = Query(default=False),
    reset_redundancy: bool = Query(default=False),
    include_outliers: bool = Query(default=False),
    outlier_sim_threshold: float = Query(default=0.35, ge=0.0, le=1.0),
    include_conflicts: bool = Query(default=False),
    dry_run: bool = Query(default=False),
):
    """Avvia l'ottimizzazione come JOB ASINCRONO e ritorna subito (no HTTP timeout).

    Il calcolo (near-duplicate dense∩sparse O(N) round-trip Qdrant, graph cleanup,
    diagnostiche) può durare minuti su collection grandi → sincrono andrebbe in timeout
    dietro il proxy. Qui la POST valida, crea un record `optimize_jobs` (status=running),
    lancia il lavoro in background e ritorna `{job_id, status:"running"}`. Il client fa
    polling su GET `/optimize/{job_id}` finché `completed`/`failed`.

    Parametri (semantica invariata, vedi _run_optimize): `dry_run` solo conteggio;
    `apply_redundancy` marca i near-duplicate (reversibile con `reset_redundancy`);
    `include_outliers`/`include_conflicts` = diagnostiche SOLA LETTURA opt-in."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    collections = [c.name for c in qdrant_client.get_collections().collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")

    params = {
        "min_score": min_score, "min_entity_len": min_entity_len,
        "drop_numeric": drop_numeric, "dense_threshold": dense_threshold,
        "include_redundancy": include_redundancy, "apply_redundancy": apply_redundancy,
        "reset_redundancy": reset_redundancy, "include_outliers": include_outliers,
        "outlier_sim_threshold": outlier_sim_threshold,
        "include_conflicts": include_conflicts, "dry_run": dry_run,
    }
    job_id = generate_id("opt_")
    now = get_timestamp()
    optimize_jobs.insert_one({
        "_id": job_id, "vector_store_id": vector_store_id, "status": "running",
        "params": params, "result": None, "error": None,
        "created_at": now, "updated_at": now, "finished_at": None,
    })
    background_tasks.add_task(_execute_optimize_job, job_id, vector_store_id, params)
    return {
        "object": "vector_store.optimize_job",
        "job_id": job_id,
        "vector_store_id": vector_store_id,
        "status": "running",
    }


@router.get("/{vector_store_id}/optimize/{job_id}")
def get_optimize_job(vector_store_id: str, job_id: str):
    """Stato di un job di ottimizzazione (polling). `running` → riprova; `completed`
    → `result` ha l'esito; `failed` → `error`. Un job running più vecchio di
    STALE_OPTIMIZE_SECONDS è considerato orfano (worker morto a metà) → failed."""
    job = optimize_jobs.find_one({"_id": job_id, "vector_store_id": vector_store_id})
    if not job:
        raise HTTPException(status_code=404, detail="Optimize job not found")
    if job.get("status") == "running" and \
            get_timestamp() - job.get("updated_at", 0) > STALE_OPTIMIZE_SECONDS:
        optimize_jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "failed", "error": "job orfano (timeout)",
                      "finished_at": get_timestamp(), "updated_at": get_timestamp()}},
        )
        job = optimize_jobs.find_one({"_id": job_id, "vector_store_id": vector_store_id})
    return {
        "object": "vector_store.optimize_job",
        "job_id": job_id,
        "vector_store_id": vector_store_id,
        "status": job.get("status"),
        "result": job.get("result"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
    }


@router.get("/{vector_store_id}/overview")
def vector_store_overview(vector_store_id: str, refresh: bool = Query(default=False)):
    """Quadro d'insieme di un vector store (pagina "Vectors"): conteggi, distribuzione
    per directory e per categoria, cluster semantici, near-duplicate, knowledge graph.

    I blocchi pesanti (cluster semantici, near-duplicate) sono **cachati** in Mongo per
    vector_store_id e invalidati automaticamente quando cambia il numero di punti
    (ingest/delete). `refresh=true` forza il ricalcolo. Gira in threadpool (def sync)."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    collections = [c.name for c in qdrant_client.get_collections().collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")

    points = qdrant_client.get_collection(vector_store_id).points_count or 0

    # Cache: valida finché NON cambia un fingerprint cheap dello stato (punti + conteggi
    # job + ultimo updated_at). Così si invalida anche su re-ingest/retry/fail a parità di
    # chunk (che non muovono points_count). Le mutazioni che non toccano i job
    # (optimize/mark_redundant) invalidano esplicitamente via overview_cache.delete_one.
    counts = _file_counts(vector_store_id)
    _last = ingestion_jobs.find_one(
        {"vector_store_id": vector_store_id}, sort=[("updated_at", -1)],
        projection={"updated_at": 1})
    fingerprint = (f"{points}:{counts.get('total')}:{counts.get('completed')}:"
                   f"{counts.get('failed')}:{counts.get('in_progress')}:"
                   f"{(_last or {}).get('updated_at', 0)}")
    cached = overview_cache.find_one({"_id": vector_store_id})
    if not refresh and cached and cached.get("fingerprint") == fingerprint:
        return {**cached["data"], "cached": True}

    dirs = list(directories_coll.find({"vector_store_id": vector_store_id}))
    by_directory = []
    for d in dirs:
        slug = d.get("slug")
        if not slug:
            continue
        n_files = len(ingestion_jobs.distinct(
            "file_id", {"vector_store_id": vector_store_id,
                        "attributes.sophia_directory_slug": slug}))
        by_directory.append({"slug": slug, "name": d.get("name", slug),
                             "points": count_points_by_slug(vector_store_id, slug),
                             "files": n_files})
    by_directory.sort(key=lambda x: x["points"], reverse=True)

    clustering = semantic_clusters(vector_store_id)
    # max_points: bound il near-duplicate (O(N) round-trip Qdrant per punto) come fa
    # semantic_clusters → l'overview resta veloce anche su collection grandi.
    near_dup = find_redundant_clusters(vector_store_id, dense_threshold=0.96, max_points=2000)
    graph = graph_stats(vector_store_id)

    data = {
        "object": "vector_store.overview",
        "vector_store_id": vector_store_id,
        "computed_at": get_timestamp(),
        "counts": {
            "points": points,
            # punti effettivamente analizzati per cluster/categorie (può essere < points
            # se la collection supera il cap di campionamento) → la UI può segnalarlo
            "points_scanned": clustering.get("points_scanned", points),
            "files": counts,
            "directories": len(by_directory),
            "categories": len(clustering.get("by_category", [])),
        },
        "by_directory": by_directory,
        "by_category": clustering.get("by_category", []),
        "semantic_clusters": clustering.get("clusters", []),
        "near_duplicates": near_dup,
        "graph": graph,
    }
    overview_cache.update_one(
        {"_id": vector_store_id},
        {"$set": {"data": data, "fingerprint": fingerprint}},
        upsert=True,
    )
    return {**data, "cached": False}


@router.get("/{vector_store_id}/graph")
async def get_vector_store_graph(
    vector_store_id: str,
    limit: int = Query(default=2000, ge=1, le=10000),
):
    """Esporta il knowledge graph del vector store come `{nodes, links, metadata}`
    per la visualizzazione force-graph. `limit` = numero massimo di nodi (gli archi
    sono tenuti solo tra i nodi esportati). Best-effort: grafo vuoto se FalkorDB è giù."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    data = await asyncio.to_thread(export_graph, vector_store_id, limit)
    return {"object": "vector_store.graph", "vector_store_id": vector_store_id, **data}


@router.get("/{vector_store_id}/schema")
def get_store_schema(vector_store_id: str):
    """Schema di estrazione (entità + relazioni) della collection.

    Il motore (GLiNER / GLiNER-relex) è zero-shot → entità e relazioni sono solo
    liste di label. Qui ogni vector store definisce il SUO dominio (contratti,
    cartelle cliniche, circolari…) senza toccare il codice. Ritorna lo schema
    effettivo (custom se impostato, altrimenti i default globali).
    """
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    collections = [c.name for c in qdrant_client.get_collections().collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")
    return {
        "object": "vector_store.schema",
        "vector_store_id": vector_store_id,
        "custom": get_schema_doc("store", vector_store_id) is not None,
        **get_effective_schema(vector_store_id),
    }


@router.put("/{vector_store_id}/schema")
def put_store_schema(vector_store_id: str, body: StoreSchemaUpdate):
    """Imposta lo schema di estrazione a livello STORE. Campi None lasciati invariati
    (eredita il default globale). Vale dal prossimo (re-)ingest dei documenti. Gli
    override più specifici (directory/sync/file) seguono lo stesso meccanismo a cascata."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    collections = [c.name for c in qdrant_client.get_collections().collections]
    if vector_store_id not in collections:
        raise HTTPException(status_code=404, detail="Vector store not found")
    try:
        set_schema(
            "store", vector_store_id, vector_store_id,
            body.entity_labels, body.relation_labels, body.relations_enabled,
            body.chunk_max_tokens,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "object": "vector_store.schema",
        "vector_store_id": vector_store_id,
        "custom": True,
        **get_effective_schema(vector_store_id),
    }


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

        # --- Delete curation provenance (best-effort) ---
        delete_collection_bodies(vector_store_id)

        # --- Delete custom extraction schemas, ogni livello (best-effort) ---
        delete_store_schemas(vector_store_id)

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
            # I file su disco sono salvati come {file_id}{ext}: un join nudo su file_id
            # (senza estensione) non matcherebbe MAI → file orfani a ogni delete dello
            # store. _find_file_path trova il file con la sua estensione reale (e valida
            # il file_id contro il path traversal).
            file_path = _find_file_path(file_id)
            file_metadata_path = os.path.join(FILES_STORAGE, f"{file_id}_metadata.json")

            file_deleted = False
            file_metadata_deleted = False

            if file_path and os.path.exists(file_path):
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

        # --- Drop overview cache doc (evita doc orfano se l'id viene riusato) ---
        overview_cache.delete_one({"_id": vector_store_id})

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
        _cols = await asyncio.to_thread(qdrant_client.get_collections)
        collections = [c.name for c in _cols.collections]
        if vector_store_id not in collections:
            raise HTTPException(status_code=404, detail="Vector store not found")

        # Usa le utility!
        file_path = await get_file_path(file_id)

        if not file_path:
            raise HTTPException(status_code=404, detail="File not found")

        # --- Delete all Qdrant points with this file_id ---
        await asyncio.to_thread(
            delete_qdrant_points,
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

        # --- Delete curation provenance for this file (best-effort) ---
        await asyncio.to_thread(purge_file_bodies, vector_store_id, file_id)

        # --- Delete job from Mongo ---
        job_delete_result = await asyncio.to_thread(
            ingestion_jobs.delete_one,
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


@router.post("/{vector_store_id}/files/{file_id}/retry")
async def retry_failed_file(vector_store_id: str, file_id: str):
    """Ri-accoda un singolo file FAILED riportando il suo job a PENDING.

    NON ricrea il job: riusa lo STESSO documento (preserva attributes/slug/
    content_hash/supersedes), azzera l'errore e lascia che il worker lo ri-pescchi
    (claim_pending_jobs). Il binario deve essere ancora su disco (il worker rilegge
    file_path con guard file-mancante). Update filtrato su status=FAILED →
    idempotente e concorrenza-safe (non tocca PENDING/PROCESSING/COMPLETED)."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")

    job = await asyncio.to_thread(
        ingestion_jobs.find_one,
        {"vector_store_id": vector_store_id, "file_id": file_id, "status": "FAILED"},
    )
    if not job:
        raise HTTPException(status_code=404, detail="No FAILED job for this file")

    res = await asyncio.to_thread(
        ingestion_jobs.update_one,
        {"_id": job["_id"], "status": "FAILED"},
        {"$set": {"status": "PENDING", "error": None, "updated_at": get_timestamp()}},
    )
    return {"object": "vector_store.file.retry", "file_id": file_id,
            "requeued": res.modified_count}


@router.post("/{vector_store_id}/retry-failed")
async def retry_failed_files(vector_store_id: str, body: Optional[Dict[str, Any]] = Body(default=None)):
    """Ri-accoda in massa tutti i file FAILED di un vector store (FAILED → PENDING).

    Opzionale `{"slug": "..."}` nel body per limitare a una sola directory (coerente
    con la vista UI per directory). Senza slug ri-prova TUTTI i FAILED dello store.
    Stesso meccanismo del retry singolo: niente re-upload, il worker ri-pesca i PENDING."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")

    flt: Dict[str, Any] = {"vector_store_id": vector_store_id, "status": "FAILED"}
    slug = (body or {}).get("slug")
    if slug:
        flt["attributes.sophia_directory_slug"] = slug

    res = await asyncio.to_thread(
        ingestion_jobs.update_many,
        flt,
        {"$set": {"status": "PENDING", "error": None, "updated_at": get_timestamp()}},
    )
    return {"object": "vector_store.retry_failed", "vector_store_id": vector_store_id,
            "slug": slug, "requeued": res.modified_count}


@router.post("/{vector_store_id}/files/{file_id}/exclude")
async def exclude_vector_store_file(
    vector_store_id: str,
    file_id: str,
    body: Optional[Dict[str, Any]] = Body(default=None),
    x_user_id: Optional[str] = Header(default=None, alias="x-user-id"),
):
    """Marca un file come EXCLUDED: vector worker, sync SharePoint e ogni source lo
    saltano, e i dati già indicizzati (Qdrant + grafo + curation) vengono rimossi.
    La sync continua a vederlo ma lo conta escluso, anche col cron. Body opzionale
    `{"reason": "..."}`. Idempotente."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    if not file_id.startswith("file-"):
        raise HTTPException(status_code=404, detail="File not found")
    reason = (body or {}).get("reason")
    res = await asyncio.to_thread(
        exclude_file, vector_store_id, file_id, reason=reason, excluded_by=x_user_id
    )
    return {"object": "vector_store.file.excluded", **res}


@router.delete("/{vector_store_id}/files/{file_id}/exclude")
async def unexclude_vector_store_file(vector_store_id: str, file_id: str):
    """Toglie l'esclusione di un file. I suoi job restano EXCLUDED: per re-ingestire
    si re-attacca (manuale) o si re-sincronizza (SharePoint, che ora non lo salta più)."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    removed = await asyncio.to_thread(unexclude_file, vector_store_id, file_id)
    if not removed:
        raise HTTPException(status_code=404, detail="File non risulta escluso")
    return {"object": "vector_store.file.unexcluded", "file_id": file_id, "unexcluded": True}


@router.get("/{vector_store_id}/excluded")
async def list_vector_store_excluded(vector_store_id: str):
    """Lista dei file esclusi dal vector store (più recenti prima)."""
    if not vector_store_id.startswith("vs_"):
        raise HTTPException(status_code=404, detail="Vector store not found")
    data = await asyncio.to_thread(list_excluded, vector_store_id)
    return {"object": "list", "data": data}


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

    # File già marcato EXCLUDED in questo store → non ri-accodare (escluso apposta;
    # per re-ingestire si toglie prima l'esclusione).
    if await asyncio.to_thread(is_excluded, vector_store_id, file_id=file_data.file_id):
        raise HTTPException(
            status_code=409,
            detail="File escluso da questo vector store — togli l'esclusione per re-ingestire",
        )

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
                usage_bytes=job.get("file_size") or 0,
                created_at=job.get("created_at", get_timestamp()),
                vector_store_id=vector_store_id,
                status=job.get("status", "COMPLETED"),
                deduplicated=True,
            )

    # Vecchi job dello stesso file logico (nome): da rimuovere a ingest COMPLETED.
    # NB: supersedes è calcolato qui alla creazione. Due attach concorrenti dello
    # STESSO filename potrebbero non vedersi a vicenda, ma il worker serializza
    # (INGEST_MAX_CONCURRENT_JOBS=1 + claim atomico) → niente cancellazione incrociata
    # dei punti. Con più worker servirebbe un lock per (vector_store_id, filename).
    supersedes = [
        j["file_id"] for j in existing
        if j.get("file_id") and j["file_id"] != file_data.file_id
    ]

    # Override di chunk a livello FILE da chunking_strategy (OpenAI-compat, opt-in):
    # se l'attach lo specifica, vince sulla cascata dir/store per questo file.
    cs = file_data.chunking_strategy if isinstance(file_data.chunking_strategy, dict) else {}
    mct = (cs.get("static") or {}).get("max_chunk_size_tokens")
    if mct:
        try:
            await asyncio.to_thread(
                set_schema, "file", file_data.file_id, vector_store_id,
                None, None, None, int(mct),
            )
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"chunking_strategy non valido: {e}")

    # 3) Crea job su Mongo
    now_ts = get_timestamp()
    job_doc = {
        "vector_store_id": vector_store_id,
        "file_id": file_data.file_id,
        "filename": filename,
        "file_size": file_metadata.get("bytes") or 0,
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
        id=file_data.file_id,
        usage_bytes=file_metadata.get("bytes") or 0,
        created_at=get_timestamp(),
        vector_store_id=vector_store_id,
        status="PENDING",
        # chunking_strategy=file_data.chunking_strategy
    )


@router.get("/{vector_store_id}/files/{file_id}")
async def get_vector_store_file(vector_store_id: str, file_id: str):
    """Download file content and chunks with paginate"""
    try:
        if not is_valid_file_id(file_id):
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
                # motivo del fallimento (se FAILED) → visibile in UI senza GET singolo
                "error": job.get("error"),
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
