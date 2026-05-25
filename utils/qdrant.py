"""Qdrant configuration"""
from typing import Dict, Any

from qdrant_client import QdrantClient
from qdrant_client import models
from qdrant_client.http.models import RecommendQuery

from qdrant_client.models import (
    OptimizersConfigDiff,
    HnswConfigDiff, VectorParams, Distance,
    SparseVectorParams, BinaryQuantization,
    BinaryQuantizationConfig, SparseIndexParams,
    Filter, FieldCondition,
    MatchValue
)

from utils.settings import DEFAULT_EMBEDDING_DIMENSION
from .embeddings import get_bge_embeddings, get_bge_reranking_docs
from .globals import to_bool, to_int
from .schemas import RankingOptions

# Import da STESSO pacchetto (utils/) - USA IL PUNTO!
from .settings import QDRANT_URL
from .logger import get_logger

# init logger on this file
logger = get_logger(__name__)

# Initialize clients
qdrant_client = QdrantClient(url=QDRANT_URL, prefer_grpc=True, check_compatibility=False, timeout=1800)


def create_qdrant_collection(store_id: str):
    qdrant_client.create_collection(
        collection_name=store_id,
        on_disk_payload=True,
        vectors_config={
            "dense": VectorParams(
                size=DEFAULT_EMBEDDING_DIMENSION,
                distance=Distance.COSINE,
                on_disk=False,  # full precision in RAM → qualità + latenza max
            ),

            # todo: lost in performance ?? => use model for ranking top_k
            # "colbert": VectorParams(
            #     size=1024,
            #     distance=Distance.COSINE,
            #     multivector_config=MultiVectorConfig(
            #         comparator=MultiVectorComparator.MAX_SIM
            #     ),
            # )
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(
                # modifier=Modifier.IDF, todo: only for bm25
                index=SparseIndexParams(
                    on_disk=True
                ),
            )
        },
        hnsw_config=HnswConfigDiff(
            m=16,
            ef_construct=200,  # 200+ = high recall (best practice Qdrant; era default 100)
            on_disk=False,     # grafo HNSW in RAM → traversal veloce
        ),
        # NIENTE quantization: corpus medio → full precision per qualità max.
        # La binary quant serve solo a scala (milioni di vettori), qui degraderebbe
        # la qualità per un risparmio RAM che non serve.
        optimizers_config=OptimizersConfigDiff(
            default_segment_number=2,  # Numero predefinito di segmenti
            indexing_threshold=20000,  # Soglia per l'indicizzazione automatica
            memmap_threshold=10000,  # Soglia per usare memmap (file mappati in memoria)
            max_segment_size=5_000_000,  # Dimensione massima del segmento in byte
        )
    )

    # Payload index sui campi filtrati. Senza, l'HNSW filtrato degrada recall e
    # performance (il filterable HNSW si costruisce solo se gli index esistono):
    # l'agent filtra SEMPRE per sophia_directory_slug → indispensabile.
    for field in ("sophia_directory_slug", "filename"):
        try:
            qdrant_client.create_payload_index(
                collection_name=store_id,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as e:
            logger.warning(f"create_payload_index({store_id}, {field}) failed: {e}")

    # Full-text index sul `text` → abilita il canale lessicale/exact-match (MatchText):
    # recupera i chunk che contengono i token ESATTI della query (codici, riferimenti,
    # acronimi) dove il dense è debole. Vedi qdrant_lexical_search.
    ensure_text_index(store_id)


# Collezioni con il full-text index già garantito (evita di ricrearlo a ogni ricerca).
_text_indexed: set = set()


def ensure_text_index(collection_name: str) -> bool:
    """Crea (best-effort, idempotente) il full-text index sul campo `text`. Necessario
    perché MatchText funzioni. Su collection esistenti l'indice si costruisce senza
    re-ingest dei punti."""
    if collection_name in _text_indexed:
        return True
    try:
        qdrant_client.create_payload_index(
            collection_name=collection_name,
            field_name="text",
            field_schema=models.TextIndexParams(
                type="text",
                tokenizer=models.TokenizerType.WORD,
                lowercase=True,
                min_token_len=2,
                max_token_len=30,
            ),
        )
        _text_indexed.add(collection_name)
        return True
    except Exception as e:
        # "already exists" → indice già presente, va bene lo stesso
        if "already" in str(e).lower() or "exist" in str(e).lower():
            _text_indexed.add(collection_name)
            return True
        logger.warning(f"ensure_text_index({collection_name}) failed: {e}")
        return False


def qdrant_lexical_search(collection_name: str, terms, search_data, limit: int = 30):
    """Canale lessicale / exact-match: ritorna i chunk il cui `text` contiene almeno
    uno dei `terms` esatti (codici, riferimenti, acronimi), nello stesso scope (slug)
    della ricerca principale. Ordinati per numero di termini trovati. Rank-based →
    pensato per essere fuso via RRF col canale vettoriale. Best-effort → [] se errore.
    """
    if not terms:
        return []
    if not ensure_text_index(collection_name):
        return []
    should = [
        models.FieldCondition(key="text", match=models.MatchText(text=t))
        for t in terms
    ]
    must = []
    base = build_qdrant_filter(search_data.filters) if getattr(search_data, "filters", None) else None
    if base is not None:
        must.append(base)
    must.append(models.Filter(should=should))  # ≥1 termine
    try:
        points, _ = qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=models.Filter(must=must),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        logger.warning(f"qdrant_lexical_search({collection_name}) failed: {e}")
        return []
    lowered = [t.lower() for t in terms]

    def _overlap(p):
        txt = ((p.payload or {}).get("text") or "").lower()
        return sum(1 for t in lowered if t in txt)

    points.sort(key=_overlap, reverse=True)
    return points


def build_qdrant_filter(filters: Dict[str, Any]) -> models.Filter:
    """
    Converte filtri in formato OpenAI-like in filtri Qdrant

    Esempi di input:
    {
        "sophia_directory": "/path/to/dir",
        "sharepoint_file_id": "123",
        "$and": [
            {"created_at": {"$gte": 1234567890}},
            {"file_size": {"$lte": 1000000}}
        ]
    }
    """

    conditions = []

    # Gestisci operatori logici
    if "$and" in filters:
        and_conditions = []
        for condition in filters["$and"]:
            and_conditions.append(build_qdrant_filter(condition))
        return models.Filter(
            must=and_conditions
        )

    if "$or" in filters:
        or_conditions = []
        for condition in filters["$or"]:
            or_conditions.append(build_qdrant_filter(condition))
        return models.Filter(
            should=or_conditions
        )

    if "$not" in filters:
        return models.Filter(
            must_not=[build_qdrant_filter(filters["$not"])]
        )

    # Gestisci condizioni semplici
    for key, value in filters.items():
        if key.startswith("$"):
            continue

        # Se il valore è un dizionario con operatori
        if isinstance(value, dict):
            for operator, operand in value.items():
                if operator == "$eq":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=operand)
                        )
                    )
                elif operator == "$ne":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchExcept(except_=operand)
                        )
                    )
                elif operator == "$gt":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            range=models.Range(gt=operand)
                        )
                    )
                elif operator == "$gte":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            range=models.Range(gte=operand)
                        )
                    )
                elif operator == "$lt":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            range=models.Range(lt=operand)
                        )
                    )
                elif operator == "$lte":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            range=models.Range(lte=operand)
                        )
                    )
                elif operator == "$in":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchAny(any=operand)
                        )
                    )
                elif operator == "$nin":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchExcept(except_=operand)
                        )
                    )
                elif operator == "$contains":
                    conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchText(text=operand)
                        )
                    )
        else:
            # Valore semplice, assume uguaglianza
            conditions.append(
                models.FieldCondition(
                    key=key,
                    match=models.MatchValue(value=value)
                )
            )

    if not conditions:
        return None

    return models.Filter(
        must=conditions
    )


def delete_qdrant_points(collection_name, field_name, field_value):
    qdrant_client.delete(
        collection_name=collection_name,
        points_selector=Filter(
            must=[
                FieldCondition(
                    key=field_name,  # key dinamica
                    match=MatchValue(value=field_value)  # valore dinamico
                )
            ]
        )
    )


def recommend_for_seed(
        collection_name: str,
        seed_point_id,
        neighbors_per_seed: int,
        filter_conditions: Any,
        include_payload: bool = True,
):
    return qdrant_client.query_points(
        collection_name=collection_name,

        # prefetch=[
        #     models.Prefetch(
        #         query=RecommendQuery(
        #             recommend=models.RecommendInput(
        #                 positive=[seed_point_id],  # ID dei punti positivi
        #                 strategy=models.RecommendStrategy.BEST_SCORE
        #             )
        #         ),
        #         using="dense",
        #         filter=filter_conditions,
        #     ),
        #     # models.Prefetch(
        #     #     query=RecommendQuery(
        #     #         recommend=models.RecommendInput(
        #     #             positive=[seed_point_id],  # ID dei punti positivi
        #     #             strategy=models.RecommendStrategy.BEST_SCORE
        #     #         )
        #     #     ),
        #     #     using="sparse",
        #     #     filter=filter_conditions,
        #     # ),
        # ],

        # # Perform reranking with Fusion
        # query=models.FusionQuery(
        #     fusion=models.Fusion.RRF,  # ?? rrf | dbsf wich one?
        # ),

        query=RecommendQuery(
            recommend=models.RecommendInput(
                positive=[seed_point_id],  # ID dei punti positivi
                strategy=models.RecommendStrategy.BEST_SCORE
            )
        ),

        using="dense",  # 👈 IMPORTANTISSIMO

        limit=neighbors_per_seed,
        query_filter=filter_conditions,
        with_payload=include_payload,
        with_vectors=False,
    )


def qdrant_hybrid_batch_search(collection_name: str, query_text: str, search_data):
    # ========== STEP PREPARA FILTRI ==========
    filter_conditions = None

    if search_data.filters:
        filter_conditions = build_qdrant_filter(search_data.filters)

    ranking_options = (search_data.ranking_options or RankingOptions()).model_dump()

    # this is a global threshold => like for rerank ? or other...
    score_threshold = ranking_options.get('score_threshold', 0.22)

    # Configurazione per score Qdrant
    dense_threshold = ranking_options.get('dense_threshold', 0.05)  # basso, per non tagliare troppo
    sparse_threshold = ranking_options.get('sparse_threshold', 0.0)  # idem, lascia respirare lo sparse
    fusion_threshold = ranking_options.get('fusion_threshold', 0.0)  # taglio leggero sul risultato fuso

    # Configurazione per limit results Qdrant
    dense_limit = ranking_options.get('dense_limit', 30)
    sparse_limit = ranking_options.get('sparse_limit', 80)
    fusion_limit = ranking_options.get('fusion_limit', 50)

    # Nuova configurazione pesi per reranker
    dense_weight = ranking_options.get('dense_weight', 0.1)
    sparse_weight = ranking_options.get('sparse_weight', 0.00)
    colbert_weight = ranking_options.get('colbert_weight', 1.0)

    # Configurazioni aggiuntive per hybrid recommend
    enable_rerank = to_bool(ranking_options.get('enable_rerank', True))
    max_rerank_results = to_int(ranking_options.get('max_rerank_results', 200))

    enable_recommendation = to_bool(ranking_options.get('enable_recommendation', False))
    max_seed_results = to_int(ranking_options.get('max_seed_results', 2))
    neighbors_per_seed = to_int(ranking_options.get('neighbors_per_seed', 2))

    mapping = {
        "rrf": models.Fusion.RRF,
        "dbsf": models.Fusion.DBSF,
    }

    raw = ranking_options.get("fusion_method", "rrf")
    fusion_method = mapping.get(raw, models.Fusion.RRF)

    # ========== STEP 4: BATCH QUERY QDRANT (TUTTI INSIEME!) ==========
    logger.info("Executing batch search on Qdrant...")

    # Generate dense embeddings per il batch | grab only one
    dense_list, sparse_list = get_bge_embeddings([query_text])

    # se è una sola query, ritorna direttamente il singolo vettore
    dense_vector = dense_list[0]
    sparse_vector = sparse_list[0]

    logger.debug(f"[PERFORM #RAG] Query:{query_text}")
    logger.debug(f"[PERFORM #RAG] Dense:{dense_threshold} | Limit:{dense_limit}")
    logger.debug(f"[PERFORM #RAG] Sparse:{sparse_threshold} | Limit:{sparse_limit}")
    logger.debug(f"[PERFORM #RAG] Fusion:{fusion_method} | Threshold:{fusion_threshold} | Limit:{fusion_limit}")
    logger.debug(f"[PERFORM #RAG] Filters:{filter_conditions}")
    logger.debug(f"[PERFORM #RAG] Rerank Weight: D{dense_weight}| S{sparse_weight}| C{colbert_weight}")

    # https://colab.research.google.com/drive/1ALDrxN8gl5Rwju9W1wKw-luz0uKzQzT0#scrollTo=vPTKmMov_sIc
    results = qdrant_client.query_batch_points(
        collection_name=collection_name,
        requests=[
            models.QueryRequest(

                # Set up prefetch for hybrid search
                prefetch=[
                    models.Prefetch(
                        query=dense_vector,
                        using="dense",
                        limit=dense_limit,
                        score_threshold=dense_threshold,
                        filter=filter_conditions,
                    ),
                    models.Prefetch(
                        query=sparse_vector,
                        using="sparse",
                        limit=sparse_limit,
                        score_threshold=sparse_threshold,
                        filter=filter_conditions,
                    ),
                ],

                # Perform reranking with Fusion
                query=models.FusionQuery(
                    fusion=fusion_method,  # ?? rrf | dbsf wich one?
                ),

                filter=filter_conditions,
                with_payload=search_data.include_metadata,
                score_threshold=fusion_threshold,
                limit=fusion_limit,

            )
        ],
    )

    all_points = []
    for batch_result in results:
        all_points.extend(batch_result.points)

    logger.info(f"Got {len(all_points)} total points from batch query")

    # ========== STEP DEDUPLICAZIONE SMART ==========
    unique_results = {}
    for point in all_points:
        point_id = str(point.id)
        if point_id not in unique_results:
            unique_results[point_id] = point

    # Sort per score decrescente (embedding similarity)
    sorted_results = sorted(unique_results.values(), key=lambda x: x.score, reverse=True)
    logger.debug(f"Primo risultato: {sorted_results[0] if sorted_results else 'empty'}")

    # disabled rerank
    if not enable_rerank:
        return sorted_results[:search_data.max_num_results]

    # ========== STEP PRENDI I DOCUMENTI PER RERANKING ==========
    # todo: non ha senso max num result per il reranker, si taglia la fusion
    rerank_candidates = sorted_results[:max_rerank_results]
    logger.info(f"Sending {len(rerank_candidates)} candidates to reranker")

    # Prepara i documenti per il reranker
    documents_to_rerank = [point.payload.get("text", "") for point in rerank_candidates]

    # Chiama il reranker (cross-encoder: scora la coppia query↔documento;
    # i vecchi weights dense/sparse/colbert sono ignorati dal servizio aggiornato).
    all_reranked_results = get_bge_reranking_docs(query_text, documents_to_rerank)

    deduplicated = {}
    for result in all_reranked_results:
        index = result["index"]

        if result["relevance_score"] < score_threshold:
            continue

        # usa il nome del campo reale restituito dal servizio
        score = result["relevance_score"]  # <-- cambia qui se si chiama diversamente

        if index not in deduplicated or score > deduplicated[index]["relevance_score"]:
            deduplicated[index] = result

    # Ordina per il campo giusto
    reranked_results = sorted(
        deduplicated.values(),
        key=lambda x: x["relevance_score"],  # <-- stesso nome di sopra
        reverse=True,
    )

    logger.info(f"Risultati dopo deduplicazione e reranking: {len(reranked_results)}")

    # Se non c'è nessun risultato, esci pulito
    if not reranked_results:
        return []

    # Mappa meta → punti Qdrant (ScoredPoint)
    seed_points = []
    for item in reranked_results:

        if item["relevance_score"] < score_threshold:
            continue

        idx = item["index"]
        if 0 <= idx < len(rerank_candidates):
            pt = rerank_candidates[idx]
            seed_points.append((pt, item["relevance_score"]))  # (ScoredPoint, rerank_score)

    # disabled recommendation
    if not enable_recommendation:
        results_to_return = []
        for point, rerank_score in seed_points[:search_data.max_num_results]:
            results_to_return.append({
                "id": str(point.id),
                "score_rerank": float(rerank_score),
                "score_qdrant": float(point.score),
                "payload": point.payload,
                "source": "seed",
            })
        return results_to_return

    # seleziona i top n
    top_seed_meta = seed_points[:max_seed_results]

    logger.info(f"Seeds selezionati: {len(top_seed_meta)}")

    # ========== STEP 6: NEIGHBORS VIA RECOMMEND PER OGNI SEED ==========
    neighbors_by_seed = {}

    for point, rerank_score in top_seed_meta:
        sid = point.id
        recs = recommend_for_seed(
            collection_name=collection_name,
            seed_point_id=sid,
            neighbors_per_seed=neighbors_per_seed,
            filter_conditions=filter_conditions,
            include_payload=search_data.include_metadata,
        )
        neighbors_by_seed[str(sid)] = recs

    # ========== STEP 7: FUSIONE FINALE (SEMI + VICINI) ==========
    final_by_id = {}

    # 7.1 Inserisco i seeds per primi, mantenendo l’ordine rerank
    for point, rerank_score in seed_points:
        pid = str(point.id)
        final_by_id[pid] = {
            "id": pid,
            "score_rerank": float(rerank_score),
            "score_qdrant": float(point.score),
            "payload": point.payload,
            "source": "seed",
        }

    # 7.2 Inserisco i vicini (recommend) se non duplicati
    for sid, recs in neighbors_by_seed.items():
        # recs è un QueryResponse, accedi a .points
        for rec_point in recs.points:  # ← Cambiato qui
            pid = str(rec_point.id)
            if pid in final_by_id:
                continue
            final_by_id[pid] = {
                "id": pid,
                "score_rerank": 0,  # fake score
                "score_qdrant": float(rec_point.score),
                "payload": rec_point.payload,
                "source": "neighbor",
            }

    # 7.3 Ordino: prima i seeds nell’ordine del rerank, poi i vicini
    ordered_final = []

    # seeds in ordine
    for point, rerank_score in seed_points:
        pid = str(point.id)
        ordered_final.append(final_by_id[pid])

    # neighbors dopo, in qualche ordine coerente (es. score_qdrant decrescente)
    neighbors_only = [
        v for v in final_by_id.values() if v["source"] == "neighbor"
    ]
    neighbors_only.sort(key=lambda x: x["score_qdrant"], reverse=True)

    ordered_final.extend(neighbors_only)

    # limita al contesto massimo da passare al LLM
    max_context = search_data.max_num_results
    final_points = ordered_final[:max_context]

    return final_points
