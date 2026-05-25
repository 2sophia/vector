"""Application fastapi models"""

from typing import Union, List, Dict, Any, Optional, Literal

from pydantic import BaseModel, Field

# Import da STESSO pacchetto (utils/) - USA IL PUNTO!
from .settings import DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
from .logger import get_logger

# init logger on this file
logger = get_logger(__name__)


class StoreSchemaUpdate(BaseModel):
    """Schema di estrazione (entità + relazioni, zero-shot) per un livello del cascade.
    Tutti opzionali: None lascia il campo invariato (eredita il livello sotto)."""
    entity_labels: Optional[List[str]] = None
    relation_labels: Optional[List[str]] = None
    relations_enabled: Optional[bool] = None


class VectorStoreCreate(BaseModel):
    name: str
    metadata: Optional[Dict[str, Any]] = {}
    expires_after: Optional[Dict[str, int]] = None


class VectorStoreUpdate(BaseModel):
    name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# class IngestStatus(str, Enum):
#     PENDING = "PENDING"
#     PROCESSING = "PROCESSING"
#     COMPLETED = "COMPLETED"
#     FAILED = "FAILED"
#     ABORTED = "ABORTED"

class RankingOptions(BaseModel):
    """Opzioni per il ranking e reranking dei risultati."""
    score_threshold: Optional[float] = 0.22

    # hybrid search parameters
    dense_limit: Optional[int] = 150  # Quanti risultati recuperare per dense
    sparse_limit: Optional[int] = 150  # Quanti risultati recuperare per sparse
    fusion_limit: Optional[int] = 250  # Quanti risultati recuperare per la fusion

    dense_threshold: Optional[float] = 0.05  # Limite semantico per dense
    sparse_threshold: Optional[float] = 0.0  # Limite semantico recuperare per sparse
    fusion_threshold: Optional[float] = 0.0  # Limite semantico recuperare per la fusion

    fusion_method: Optional[Literal["rrf", "dbsf"]] = Field(
        default="rrf",
        description="Metodo di fusione: 'rrf' (Reciprocal Rank Fusion) o 'dbsf' (Distribution-Based Score Fusion)"
    )

    # Reranking parameters
    enable_rerank: Optional[bool] = True
    max_rerank_results: Optional[int] = 200  # Quanti risultati al rerank

    # Retrival reranker params
    dense_weight: Optional[float] = 0.1
    sparse_weight: Optional[float] = 0.00
    colbert_weight: Optional[float] = 1.0

    enable_recommendation: Optional[bool] = True  ## retrival augmentation
    max_seed_results: Optional[int] = 3
    neighbors_per_seed: Optional[int] = 2


class VectorSearch(BaseModel):
    query: str  # Ora accetta stringa
    # file_id: Optional[str] = None  # Campo per il file_id

    max_num_results: Optional[int] = 15

    ranking_options: Optional[RankingOptions] = Field(
        default_factory=RankingOptions,
        description="Opzioni dettagliate per il ranking"
    )

    filters: Optional[Dict[str, Any]] = {}

    # Graph-augmented retrieval (M4): espande i risultati col knowledge graph
    # (chunk che condividono entità + adiacenti via :NEXT) e ri-rerankizza.
    # Default False → comportamento search invariato (retrocompat agent).
    graph_expand: Optional[bool] = False
    graph_neighbors: Optional[int] = 20      # max chunk di vicinato aggiunti
    graph_df_max: Optional[float] = 0.5      # entità in >df_max dei chunk = stopword-entity, escluse dall'espansione

    # Response options
    include_metadata: Optional[bool] = True
    include_vectors: Optional[bool] = False  # Include i vettori nella risposta
    include_distances: Optional[bool] = True  # Include le distanze/score


class SearchExplanation(BaseModel):
    """Explanation model per la risposta"""
    strategy: str
    search_type: str
    queries_processed: int
    total_points_retrieved: int
    unique_points_after_dedup: int
    final_results_returned: int
    dedup_strategy: str


class SearchResponse(BaseModel):
    """Response model per la search"""
    object: str
    data: List[Dict[str, Any]]
    query: Union[str, List[str]]
    usage: Dict[str, int]
    explanation: Optional[SearchExplanation] = None  # ← Tipizzato correttamente


class FileAttach(BaseModel):
    file_id: str
    chunking_strategy: Optional[Dict[str, Any]] = {
        "type": "static",
        "static": {
            "max_chunk_size_tokens": DEFAULT_CHUNK_SIZE,
            "chunk_overlap_tokens": DEFAULT_CHUNK_OVERLAP
        }
    }
    attributes: Optional[Dict[str, Any]] = {}


class VectorStore(BaseModel):
    id: str
    object: str = "vector_store"
    name: str
    status: str
    usage_bytes: int
    created_at: int
    file_counts: Dict[str, int]
    metadata: Dict[str, Any]
    expires_after: Optional[Dict[str, int]] = None
    expires_at: Optional[int] = None
    last_active_at: Optional[int] = None


class FileObject(BaseModel):
    id: str
    object: str = "file"
    bytes: int
    created_at: int
    filename: str
    purpose: str
    status: str = "uploaded"
    status_details: Optional[str] = None


class VectorStoreFile(BaseModel):
    id: str
    job_id: str
    object: str = "vector_store.file"
    usage_bytes: int
    created_at: int
    vector_store_id: str
    status: Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]
    last_error: Optional[Dict[str, Any]] = None
    # True se l'attach è stato saltato perché il contenuto era già presente.
    deduplicated: Optional[bool] = None
    # chunking_strategy: Optional[Dict[str, Any]] = None


class IngestionJobResponse(BaseModel):
    job_id: str
    status: Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]
    vector_store_id: str
    file_id: str


# ================== INGESTION SOURCES ==================
# Una "source" è una connessione configurabile da cui ingerire documenti
# (es. un sito SharePoint con le proprie credenziali). Permette più sorgenti
# con credenziali diverse, invece di un'unica config hardcoded via env.

class SourceCreate(BaseModel):
    name: str
    # Tipo provider (validato contro il registry: sharepoint, gdrive, s3, ...).
    type: str = "sharepoint"
    # Config polimorfica per tipo: i campi dipendono dal provider (vedi
    # utils/sources). I campi `secret` vengono cifrati at-rest dal router.
    config: Dict[str, Any] = {}


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    status: Optional[Literal["active", "disabled"]] = None


class SourceResponse(BaseModel):
    id: str
    object: str = "ingestion_source"
    name: str
    type: str
    status: str
    # config senza il secret: espone solo i campi non sensibili + secret_set
    config: Dict[str, Any]
    secret_set: bool
    created_at: int
    updated_at: int


# ================== DIRECTORIES ==================
# Astrazione user-facing del prodotto open: una "directory" raggruppa file con
# uno slug e custom properties. Sotto il cofano lo slug è `sophia_directory_slug`
# e le properties vengono applicate (top-level) a ogni chunk dei file caricati.
# Più directory vivono in uno stesso vector store (collezione Qdrant).

class DirectoryCreate(BaseModel):
    name: str
    # Se assente, derivato dal name (slugify). Immutabile dopo la creazione.
    slug: Optional[str] = None
    properties: Optional[Dict[str, Any]] = {}
    # Se assente, usa il vector store di default.
    vector_store_id: Optional[str] = None


class DirectoryUpdate(BaseModel):
    name: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None


class DirectoryResponse(BaseModel):
    id: str
    object: str = "directory"
    name: str
    slug: str
    properties: Dict[str, Any]
    vector_store_id: str
    file_count: int
    created_at: int
    updated_at: int
