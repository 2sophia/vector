"""
MCP server (/mcp) — espone Sophia Vector come server Model Context Protocol.

Un agent compatibile MCP (Claude, sophia-agent, qualunque client) si collega a /mcp e
ottiene RAG + navigazione + NLP come **tool** nativi, più un **prompt** di workflow:

  Retrieval / navigazione
    - search            → hybrid search su un vector store (il tool RAG; drill-in per file/cartella)
    - list_vector_stores / list_directories → discovery (cosa c'è, come è organizzato)
    - list_files        → catalogo navigabile a lista (filtrabile per nome) — "orienta prima di cercare"
    - search_by_name    → trova un documento per nome
    - get_document      → testo pieno di un documento (deep-dive quando i chunk non bastano)
  NLP
    - extract_entities / classify_text / extract_relations → i modelli del backend

  Prompt
    - rag_research      → template di workflow agnostico (orienta → cerca → drilla → cita)

I tool chiamano le STESSE funzioni interne degli endpoint /v1/* (stesso processo → zero HTTP
interno, nessun doppione di logica né di modelli). Sono tutti **read-only** (annotati). Trasporto
streamable-HTTP montato su /mcp in main.py (streamable_http_path="/" per evitare il doppio
prefisso del mount); il session_manager gira nel suo lifespan e l'API key di /v1/* protegge
anche /mcp (middleware in main.py). Spegnibile via SOPHIA_VECTOR_MCP_ENABLED.
"""

import asyncio
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from utils.logger import get_logger
from utils.schemas import VectorSearch

logger = get_logger(__name__)

# streamable_http_path="/" — l'handler interno sta su "/", non sul default "/mcp": montandolo
# su "/mcp" in main.py l'endpoint finale è /mcp (non /mcp/mcp, il doppio prefisso del mount).
mcp = FastMCP("Sophia Vector", stateless_http=True, json_response=True, streamable_http_path="/")

# Tutti i tool sono letture/estrazioni: nessuno muta dati. L'annotation lo dichiara al client
# (l'agent sa che sono sicuri da chiamare). open-world: interrogano un datastore/modello esterno.
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


# --------------------------------------------------------------------------- #
# Retrieval                                                                    #
# --------------------------------------------------------------------------- #

@mcp.tool(
    annotations=_RO,
    description=(
        "Ricerca semantica (hybrid dense+sparse + rerank) sui documenti di un vector store. "
        "È il tool RAG: usalo per trovare i passaggi rilevanti a una domanda. Passa "
        "`vector_store_id` (vedi list_vector_stores) e la `query` in linguaggio naturale, **un "
        "concetto per query**. Per approfondire un documento già individuato, passa `filename` "
        "(drill-in: più efficace di una nuova query ampia); `directory_slug` restringe a una "
        "cartella. `graph_expand=true` espande coi documenti collegati nel knowledge graph. "
        "Ritorna i chunk più pertinenti con testo, file di provenienza e score."
    ),
)
async def search(
    vector_store_id: str,
    query: str,
    max_results: int = 8,
    filename: Optional[str] = None,
    directory_slug: Optional[str] = None,
    graph_expand: bool = False,
) -> Dict[str, Any]:
    from routers.search import search_vector_store  # import locale: evita cicli all'avvio

    # drill-in: filtri Qdrant su campi indicizzati del payload (filename / directory)
    clauses = []
    if filename:
        clauses.append({"filename": filename})
    if directory_slug:
        clauses.append({"sophia_directory_slug": directory_slug})
    filters: Dict[str, Any] = (
        clauses[0] if len(clauses) == 1 else {"$and": clauses} if clauses else {}
    )

    search_data = VectorSearch(
        query=query,
        max_num_results=max(1, min(int(max_results), 50)),
        graph_expand=graph_expand,
        filters=filters,
    )
    try:
        # search_vector_store è sincrona e fa I/O bloccante (Qdrant/embeddings/rerank)
        res = await asyncio.to_thread(search_vector_store, vector_store_id, search_data)
    except Exception as e:
        # HTTPException (404 store inesistente) o errori interni → messaggio leggibile per l'agent
        detail = getattr(e, "detail", None) or str(e)
        return {"error": str(detail), "vector_store_id": vector_store_id, "results": []}

    hits = [
        {
            "score": d.get("score"),
            "filename": d.get("filename"),
            "file_id": d.get("file_id"),
            "content": d.get("content"),
        }
        for d in res.get("data", [])
    ]
    return {"query": query, "vector_store_id": vector_store_id, "count": len(hits), "results": hits}


@mcp.tool(
    annotations=_RO,
    description=(
        "Elenca i vector store disponibili (le collezioni indicizzate). Usalo prima di search "
        "per scoprire su quale `vector_store_id` cercare. Ritorna id, nome e conteggi file."
    ),
)
async def list_vector_stores() -> Dict[str, Any]:
    from routers.vector_stores import list_vector_stores as _list

    res = await _list(limit=50, order="desc", after=None)
    stores = [
        {
            "id": getattr(s, "id", None),
            "name": getattr(s, "name", None),
            "file_counts": getattr(s, "file_counts", None),
        }
        for s in res.get("data", [])
    ]
    return {"count": len(stores), "vector_stores": stores}


@mcp.tool(
    annotations=_RO,
    description=(
        "Elenca le directory (raggruppamenti logici di documenti). Opzionale `vector_store_id` "
        "per filtrare a un solo store. Utile per capire come è organizzato il corpus."
    ),
)
async def list_directories(vector_store_id: Optional[str] = None) -> Dict[str, Any]:
    from routers.directories import list_directories as _list

    res = await _list(vector_store_id=vector_store_id)
    dirs = [
        {
            "id": getattr(d, "id", None),
            "name": getattr(d, "name", None),
            "slug": getattr(d, "slug", None),
            "vector_store_id": getattr(d, "vector_store_id", None),
            "file_count": getattr(d, "file_count", None),
        }
        for d in res.get("data", [])
    ]
    return {"count": len(dirs), "directories": dirs}


@mcp.tool(
    annotations=_RO,
    description=(
        "Catalogo dei documenti di un vector store (a lista). `name_contains` filtra per "
        "sottostringa del nome file (case-insensitive). Orientarsi col catalogo PRIMA di cercare "
        "è quasi gratis e rende la search molto più mirata. Ritorna nome, file_id, n. chunk e stato."
    ),
)
async def list_files(
    vector_store_id: str, name_contains: Optional[str] = None, limit: int = 100
) -> Dict[str, Any]:
    from routers.vector_stores import list_vector_store_files

    try:
        res = await asyncio.to_thread(list_vector_store_files, vector_store_id)
    except Exception as e:
        return {"error": str(getattr(e, "detail", None) or e), "files": []}

    needle = (name_contains or "").lower()
    files = [
        {
            "file_id": f.get("file_id"),
            "filename": f.get("filename"),
            "num_chunks": f.get("num_chunks"),
            "status": f.get("status"),
        }
        for f in res.get("data", [])
        if not needle or needle in (f.get("filename") or "").lower()
    ]
    return {"vector_store_id": vector_store_id, "count": len(files), "files": files[: max(1, min(int(limit), 500))]}


@mcp.tool(
    annotations=_RO,
    description=(
        "Trova un documento per nome (sottostringa, case-insensitive) in un vector store. Più "
        "veloce della ricerca semantica quando conosci il nome o un codice del file. Ritorna i "
        "file corrispondenti con file_id e n. chunk; poi usa get_document o search(filename=…)."
    ),
)
async def search_by_name(vector_store_id: str, name: str) -> Dict[str, Any]:
    from routers.vector_stores import list_vector_store_files

    try:
        res = await asyncio.to_thread(list_vector_store_files, vector_store_id)
    except Exception as e:
        return {"error": str(getattr(e, "detail", None) or e), "matches": []}

    needle = (name or "").lower().strip()
    matches = [
        {
            "file_id": f.get("file_id"),
            "filename": f.get("filename"),
            "num_chunks": f.get("num_chunks"),
            "status": f.get("status"),
        }
        for f in res.get("data", [])
        if needle and needle in (f.get("filename") or "").lower()
    ]
    return {"vector_store_id": vector_store_id, "count": len(matches), "matches": matches}


@mcp.tool(
    annotations=_RO,
    description=(
        "Testo COMPLETO di un documento, ricostruito dai suoi chunk in ordine di lettura. Usalo "
        "per il deep-dive quando i chunk della search sono pertinenti ma parziali (un valore a "
        "metà paragrafo, una lista che continua, un articolo da citare per intero). `max_chars` "
        "tronca l'output (default 20000). Passa il `file_id` ottenuto da search/list_files."
    ),
)
async def get_document(
    vector_store_id: str, file_id: str, max_chars: int = 20000
) -> Dict[str, Any]:
    from qdrant_client import models
    from utils import qdrant_client

    def _fetch() -> Dict[str, Any]:
        flt = models.Filter(
            must=[models.FieldCondition(key="file_id", match=models.MatchValue(value=file_id))]
        )
        points, _ = qdrant_client.scroll(
            collection_name=vector_store_id, scroll_filter=flt,
            limit=10000, with_payload=True, with_vectors=False,
        )
        if not points:
            return {"error": "documento non trovato", "file_id": file_id, "text": ""}
        # ordina per chunk_index (ordine di lettura) e concatena il testo
        points.sort(key=lambda p: (p.payload or {}).get("chunk_index", 0))
        filename = (points[0].payload or {}).get("filename")
        text = "\n\n".join((p.payload or {}).get("text", "") for p in points)
        truncated = len(text) > max_chars
        return {
            "file_id": file_id, "filename": filename, "num_chunks": len(points),
            "truncated": truncated, "text": text[: max(0, int(max_chars))],
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        return {"error": str(getattr(e, "detail", None) or e), "file_id": file_id, "text": ""}


# --------------------------------------------------------------------------- #
# NLP (stesse istanze del router /v1/nlp — una copia sola dei modelli)         #
# --------------------------------------------------------------------------- #

@mcp.tool(
    annotations=_RO,
    description=(
        "Estrazione entità zero-shot (GLiNER) + regex ad alta precisione (IBAN, importi, "
        "riferimenti normativi…). `labels` opzionale (CSV di tipi entità) altrimenti usa lo "
        "schema di default. Ritorna le entità trovate nel testo."
    ),
)
async def extract_entities(text: str, labels: Optional[List[str]] = None) -> Dict[str, Any]:
    from routers.nlp import _ner

    if await asyncio.to_thread(_ner.load) is None:
        return {"error": "GLiNER non disponibile su questo deployment", "entities": []}
    results = await asyncio.to_thread(_ner.extract, [text], labels)
    return {"entities": results[0] if results else []}


@mcp.tool(
    annotations=_RO,
    description=(
        "Classificazione zero-shot del testo (GliClass) su label arbitrarie. `labels` opzionale "
        "(CSV) altrimenti usa quelle di default. Ritorna [{label, score}] sopra soglia."
    ),
)
async def classify_text(
    text: str, labels: Optional[List[str]] = None, threshold: Optional[float] = None
) -> Dict[str, Any]:
    from routers.nlp import _classifier

    if await asyncio.to_thread(_classifier.load) is None:
        return {"error": "Classifier (GliClass) non disponibile", "classes": []}
    results = await asyncio.to_thread(_classifier.classify, [text], labels, threshold)
    return {"classes": results[0] if results else []}


@mcp.tool(
    annotations=_RO,
    description=(
        "Estrazione di relazioni tipizzate zero-shot (GLiNER-relex): triple (head, relazione, tail) "
        "tra le entità del testo. `entity_labels`/`relation_labels` opzionali. Utile per costruire "
        "o interrogare il knowledge graph."
    ),
)
async def extract_relations(
    text: str,
    entity_labels: Optional[List[str]] = None,
    relation_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from routers.nlp import _relex

    if await asyncio.to_thread(_relex.load) is None:
        return {"error": "GLiNER-relex non disponibile su questo deployment", "relations": []}
    results = await asyncio.to_thread(_relex.extract, [text], entity_labels, relation_labels)
    return {"relations": results[0] if results else []}


# --------------------------------------------------------------------------- #
# Prompt — workflow agnostico riutilizzabile ("skill" portabile via MCP)       #
# --------------------------------------------------------------------------- #

@mcp.prompt(
    title="RAG research workflow",
    description="Workflow per rispondere a una domanda interrogando un vector store, citando le fonti.",
)
def rag_research(question: str, vector_store_id: str = "") -> str:
    """Template agnostico: nessun assunto di dominio, solo il metodo di ricerca efficace."""
    target = f"nel vector store `{vector_store_id}`" if vector_store_id else "scegliendo il vector store con list_vector_stores"
    return f"""Rispondi a questa domanda interrogando la knowledge base {target}, usando i tool MCP di Sophia Vector. Segui questo metodo:

DOMANDA: {question}

1. ORIENTAMENTO (quasi gratis): usa `list_files` (con `name_contains` se la domanda suggerisce un nome) e/o `list_directories` per capire QUALI documenti esistono sul tema prima di cercare. Se conosci un nome/codice preciso, `search_by_name`.

2. RICERCA semantica con `search`: una query = UN concetto, 4–8 parole, in una sola lingua. Niente date/nomi propri nella query (per quelli filtra). Valuta gli score: >0.65 molto rilevante, 0.55–0.65 rilevante, <0.45 ignora. Tieniti un budget basso (≈4 ricerche): se non basta, riformula con sinonimi o sotto-domande invece di ripetere.

3. DRILL-IN: quando un documento è rilevante ma i chunk sono parziali, NON lanciare nuove query ampie — usa `search(filename=…)` per cercare dentro quel file, o `get_document(file_id=…)` per leggerne il testo completo.

4. SINTESI: rispondi USANDO SOLO ciò che hai trovato. Cita sempre la fonte (il `filename` dei risultati). Non inventare, non riempire i vuoti con conoscenza generica, non citare documenti che non compaiono nei risultati. Se dopo il budget non hai abbastanza, dillo e indica cosa hai provato."""
