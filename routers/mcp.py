"""
MCP server — espone Sophia Vector come server Model Context Protocol.

Due superfici, **stessa identica logica** (i tool sono definiti UNA volta sola, in funzioni
`_core_*`, e registrate su entrambe da un'unica tabella `_TOOLS`):

  • /mcp  (globale)  → discovery multi-store: ogni tool prende `vector_store_id` come
    parametro; include `list_vector_stores`. È il default per un agent che lavora su più store.

  • /v1/vector_stores/{id}/mcp  (scoped) → il vector store è **fissato dal path**: i tool
    NON espongono `vector_store_id` (lo schema non lo mostra all'agent), il vs è preso da
    `ctx.request_context.request.path_params['vsid']`. Per un agent dedicato a UNA knowledge
    base: lo instradi una volta e "cerca e basta", senza poter sbagliare lo store.

Toolkit (read-only, tutto in-process sulle stesse funzioni di /v1/* → zero HTTP interno,
una sola copia dei modelli):

  Orientamento / navigazione
    - corpus_overview   → quadro d'insieme PRIMA di cercare: temi, categorie, conteggi, grafo
    - list_vector_stores / list_directories → discovery (solo list_vector_stores è global-only)
    - list_files        → catalogo navigabile (filtrabile per nome), con stato/errore/attributi
    - search_by_name    → trova un documento per nome
  Retrieval
    - search            → hybrid dense+sparse + rerank; drill-in per file/cartella; graph_expand;
                          score_threshold per la ricerca "chirurgica"; ritorna provenienza citabile
    - get_document      → testo pieno di un documento, paginabile per chunk
  NLP
    - extract_entities / classify_text / extract_relations → i modelli del backend
  Prompt
    - rag_research      → workflow agnostico (orienta → cerca → drilla → cita)

Aggiungere un tool = scrivere UN core `_core_*` + UNA riga in `_TOOLS`: entrambe le superfici
lo ereditano. Trasporto streamable-HTTP montato in main.py; i session_manager girano nel
lifespan; l'API key di /v1/* protegge anche /mcp e /v1/vector_stores/*/mcp (middleware in
main.py). Spegnibile via SOPHIA_VECTOR_MCP_ENABLED.
"""

import asyncio
import functools
import inspect
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations

from utils.logger import get_logger
from utils.schemas import VectorSearch, RankingOptions

logger = get_logger(__name__)

# Tutti i tool sono letture/estrazioni: nessuno muta dati. L'annotation lo dichiara al client
# (l'agent sa che sono sicuri da chiamare). open-world: interrogano un datastore/modello esterno.
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

# Campi di provenienza del payload utili a un agent (citazioni + ragionamento sulla fiducia):
# proiettati DALL'attributes della search — NON un passthrough cieco (escluderebbe i campi
# interni rumorosi come body-hash / flag di curation).
_PROV_KEYS = ("page_numbers", "headings", "category", "chunk_index")


# --------------------------------------------------------------------------- #
# Access control per-directory (lo "scope" = lista di slug consentiti).
# Sulle query Qdrant diventa un must-filter ($or sugli slug); sui cataloghi
# (lista file da Mongo) un match sull'attributo. None/[] = nessuno scope
# (comportamento storico delle superfici admin/MCP). È OPT-IN: i tool restano
# identici se `directories` non è passato; la superficie REST /kb lo rende
# obbligatorio (vedi routers/kb.py) per l'enforcement non-bypassabile.
# --------------------------------------------------------------------------- #

def _dir_clause(directories: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    """Clause `filters` (stile build_qdrant_filter) per lo scope directory."""
    dirs = [d for d in (directories or []) if d]
    if not dirs:
        return None
    if len(dirs) == 1:
        return {"sophia_directory_slug": dirs[0]}
    return {"$or": [{"sophia_directory_slug": d} for d in dirs]}


def _in_dirs(attributes: Optional[Dict[str, Any]], directories: Optional[List[str]]) -> bool:
    """True se il file (dai suoi attributes) è in una delle directory consentite.
    Scope vuoto → sempre True (nessun filtro)."""
    allowed = {d for d in (directories or []) if d}
    if not allowed:
        return True
    return ((attributes or {}).get("sophia_directory_slug")) in allowed


def _err(e: Exception, **extra: Any) -> Dict[str, Any]:
    """Errore leggibile per l'agent + `status` machine-readable (404 → not_found, resto →
    backend_error) così l'agent decide se correggere l'id o riprovare.

    Per i 404 il `detail` è controllato lato codice ('Vector store not found'). Per QUALSIASI
    altra eccezione NON esponiamo `str(e)` al client MCP: potrebbe contenere host/porte/path
    interni (es. errore di connessione Qdrant gRPC) → la logghiamo server-side e restituiamo un
    messaggio generico, coerente con l'unhandled_exception_handler dell'app che scrubba i 500."""
    if getattr(e, "status_code", None) == 404:
        return {"error": str(getattr(e, "detail", None) or "not found"), "status": "not_found", **extra}
    logger.exception("MCP tool backend error: %s", e)
    return {"error": "errore interno del backend", "status": "backend_error", **extra}


# =========================================================================== #
# CORE — unica fonte di verità. Ogni funzione store-scoped prende vector_store_id
# come PRIMO parametro; la variante scoped (vs dal path) è generata da _make_scoped.
# =========================================================================== #

async def _core_search(
    vector_store_id: str,
    query: str,
    max_results: int = 8,
    filename: Optional[str] = None,
    directory_slug: Optional[str] = None,
    graph_expand: bool = False,
    score_threshold: Optional[float] = None,
    directories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from routers.search import search_vector_store  # import locale: evita cicli all'avvio

    # drill-in: filtri Qdrant su campi indicizzati del payload (filename / directory)
    clauses = []
    if filename:
        clauses.append({"filename": filename})
    if directory_slug:
        clauses.append({"sophia_directory_slug": directory_slug})
    # access control: lo scope directory è un must-filter (ANDed) → pre-filtro Qdrant,
    # vale anche per i canali grafo/lessicale. NON è bypassabile post-hoc.
    dir_clause = _dir_clause(directories)
    if dir_clause:
        clauses.append(dir_clause)
    filters: Dict[str, Any] = (
        clauses[0] if len(clauses) == 1 else {"$and": clauses} if clauses else {}
    )

    search_data = VectorSearch(
        query=query,
        max_num_results=max(1, min(int(max_results), 50)),
        graph_expand=graph_expand,
        filters=filters,
        # esplicito: la provenienza citabile (page/headings/_source/_via) vive dentro attributes,
        # popolato SOLO se include_metadata → blindiamo il contratto da un cambio di default futuro
        include_metadata=True,
    )
    # score_threshold opzionale: muta il solo campo (ranking_options è garantito dal
    # default_factory) → non sovrascrive altri parametri di ranking. Se None resta 0.25 (tarato).
    if score_threshold is not None:
        search_data.ranking_options.score_threshold = max(0.0, min(float(score_threshold), 1.0))

    try:
        # search_vector_store è sincrona e fa I/O bloccante (Qdrant/embeddings/rerank)
        res = await asyncio.to_thread(search_vector_store, vector_store_id, search_data)
    except Exception as e:
        # HTTPException (404 store inesistente) o errori interni → messaggio leggibile per l'agent
        return _err(e, vector_store_id=vector_store_id, results=[])

    hits = []
    for d in res.get("data", []):
        attrs = d.get("attributes") or {}
        hit = {
            "id": d.get("id"),                       # point id → pivot al grafo / drill-in
            "score": d.get("score"),                 # score finale (rerank se c'è)
            "score_qdrant": d.get("score_qdrant"),   # score grezzo Qdrant (gap retrieval↔rerank)
            "filename": d.get("filename"),
            "file_id": d.get("file_id"),
            "content": d.get("content"),
            # provenienza per citare la fonte esatta e ragionare sul canale
            "directory_slug": attrs.get("sophia_directory_slug"),
            "source": attrs.get("_source"),          # qdrant | graph:mentions | graph:next | lexical
            "via": attrs.get("_via"),                # entità-ponte (se via grafo)
        }
        for k in _PROV_KEYS:
            if k in attrs:
                hit[k] = attrs[k]
        hits.append(hit)
    return {"query": query, "vector_store_id": vector_store_id, "count": len(hits), "results": hits}


async def _core_corpus_overview(
    vector_store_id: str, refresh: bool = False, directories: Optional[List[str]] = None
) -> Dict[str, Any]:
    from routers.vector_stores import vector_store_overview

    # Vista SCOPED (access control): l'overview store-wide espone i `topics` (cluster
    # globali) coi nomi file di TUTTE le directory → leak. Con uno scope attivo ritorno
    # una vista sicura — SOLO le directory consentite con conteggi (file da Mongo, punti
    # da Qdrant count filtrato per slug), niente clustering/near-dup/grafo store-wide.
    allowed = [d for d in (directories or []) if d]
    if allowed:
        from utils.qdrant import count_points_by_slug
        try:
            dres = await _core_list_directories(vector_store_id, directories=allowed)
            meta = {d.get("slug"): d for d in dres.get("directories", [])}
            dirs, tot_points, tot_files = [], 0, 0
            for slug in allowed:
                pts = await asyncio.to_thread(count_points_by_slug, vector_store_id, slug)
                m = meta.get(slug, {})
                fc = m.get("file_count") or 0
                tot_points += pts
                tot_files += fc
                dirs.append({
                    "slug": slug, "name": m.get("name") or slug,
                    "file_count": fc, "points": pts, "properties": m.get("properties"),
                })
            return {
                "vector_store_id": vector_store_id, "scoped": True,
                "directories": dirs,
                "counts": {"directories": len(dirs), "files": tot_files, "points": tot_points},
            }
        except Exception as e:
            return _err(e, vector_store_id=vector_store_id)

    try:
        ov = await asyncio.to_thread(vector_store_overview, vector_store_id, refresh=refresh)
    except Exception as e:
        return _err(e, vector_store_id=vector_store_id)

    counts = ov.get("counts", {}) or {}
    files = counts.get("files", {}) or {}
    # vista trimmed: l'overview grezzo è grande (array interi di cluster/near-dup) → mangia contesto
    topics = [
        {
            "label": c.get("label"),
            "top_heading": c.get("top_heading"),
            "size": c.get("size"),
            "doc_count": c.get("doc_count"),
            "top_files": [f.get("filename") for f in (c.get("top_files") or [])],
        }
        for c in (ov.get("semantic_clusters") or [])[:15]
    ]
    nd = ov.get("near_duplicates", {}) or {}
    g = ov.get("graph", {}) or {}
    return {
        "vector_store_id": vector_store_id,
        "counts": {
            "points": counts.get("points"),
            "files": {
                "total": files.get("total"),
                "completed": files.get("completed"),
                "in_progress": files.get("in_progress"),
                "failed": files.get("failed"),
            },
            "directories": counts.get("directories"),
            "categories": counts.get("categories"),
        },
        "by_directory": ov.get("by_directory", []),
        "by_category": ov.get("by_category", []),
        "topics": topics,  # temi trasversali (KMeans sui dense) — orientamento prima di cercare
        "near_duplicates": {"redundant": nd.get("redundant"), "reduction_pct": nd.get("reduction_pct")},
        "graph": (
            {
                "documents": g.get("documents"),
                "chunks": g.get("chunks"),
                "entities": g.get("entities"),
                "relations": g.get("relations"),
            }
            if g.get("graph_enabled")
            else {"graph_enabled": False}
        ),
        "cached": ov.get("cached"),
    }


async def _core_list_vector_stores() -> Dict[str, Any]:
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


async def _core_list_directories(
    vector_store_id: Optional[str] = None, directories: Optional[List[str]] = None
) -> Dict[str, Any]:
    from routers.directories import list_directories as _list

    try:
        res = await _list(vector_store_id=vector_store_id)
    except Exception as e:
        return _err(e, directories=[])
    allowed = {d for d in (directories or []) if d}
    dirs = [
        {
            "id": getattr(d, "id", None),
            "name": getattr(d, "name", None),
            "slug": getattr(d, "slug", None),
            "vector_store_id": getattr(d, "vector_store_id", None),
            "file_count": getattr(d, "file_count", None),
            # properties: metadati di dominio (anno/tipo/owner…), propagati top-level nel
            # payload Qdrant → l'agent può usarli per decidere dove cercare o come filtrare.
            "properties": getattr(d, "properties", None),
        }
        for d in res.get("data", [])
        # access control: con lo scope attivo, mostra SOLO le directory consentite.
        if not allowed or getattr(d, "slug", None) in allowed
    ]
    return {"count": len(dirs), "directories": dirs}


def _project_file(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file_id": f.get("file_id"),
        "filename": f.get("filename"),
        "num_chunks": f.get("num_chunks"),
        "status": f.get("status"),
        "error": f.get("error"),               # se FAILED: perché → l'agent non vede "manca" ma "fallito"
        "attributes": f.get("attributes") or {},  # slug + custom properties (filtrabili)
    }


async def _core_list_files(
    vector_store_id: str, name_contains: Optional[str] = None, limit: int = 100,
    directories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from routers.vector_stores import list_vector_store_files

    try:
        res = await asyncio.to_thread(list_vector_store_files, vector_store_id)
    except Exception as e:
        return _err(e, files=[])

    needle = (name_contains or "").lower()
    files = [
        _project_file(f)
        for f in res.get("data", [])
        if (not needle or needle in (f.get("filename") or "").lower())
        and _in_dirs(f.get("attributes"), directories)  # access control
    ]
    return {
        "vector_store_id": vector_store_id,
        "count": len(files),
        "files": files[: max(1, min(int(limit), 500))],
    }


async def _core_search_by_name(
    vector_store_id: str, name: str, limit: int = 50,
    directories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from routers.vector_stores import list_vector_store_files

    try:
        res = await asyncio.to_thread(list_vector_store_files, vector_store_id)
    except Exception as e:
        return _err(e, matches=[])

    needle = (name or "").lower().strip()
    matches = [
        _project_file(f)
        for f in res.get("data", [])
        if needle and needle in (f.get("filename") or "").lower()
        and _in_dirs(f.get("attributes"), directories)  # access control
    ]
    return {
        "vector_store_id": vector_store_id,
        "count": len(matches),
        "matches": matches[: max(1, min(int(limit), 200))],
    }


async def _core_get_document(
    vector_store_id: str,
    file_id: str,
    max_chars: int = 20000,
    from_chunk: Optional[int] = None,
    to_chunk: Optional[int] = None,
    directories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from qdrant_client import models
    from utils import qdrant_client

    def _fetch() -> Dict[str, Any]:
        # valida la collection PRIMA dello scroll: su store inesistente Qdrant solleverebbe
        # un errore grezzo poco leggibile → qui ritorniamo un not_found pulito.
        if not str(vector_store_id).startswith("vs_") or vector_store_id not in [
            c.name for c in qdrant_client.get_collections().collections
        ]:
            return {"error": f"vector store '{vector_store_id}' inesistente",
                    "status": "not_found", "file_id": file_id, "text": ""}

        # access control: oltre al file_id, vincola lo scroll alle directory consentite
        # → un file_id fuori scope non restituisce punti = not_found (niente leak).
        must = [models.FieldCondition(key="file_id", match=models.MatchValue(value=file_id))]
        allowed = [d for d in (directories or []) if d]
        if allowed:
            must.append(models.Filter(should=[
                models.FieldCondition(key="sophia_directory_slug", match=models.MatchValue(value=d))
                for d in allowed
            ]))
        flt = models.Filter(must=must)
        points, _ = qdrant_client.scroll(
            collection_name=vector_store_id, scroll_filter=flt,
            limit=10000, with_payload=True, with_vectors=False,
        )
        if not points:
            return {"error": "documento non trovato", "status": "not_found",
                    "file_id": file_id, "text": ""}

        # ordina per chunk_index (ordine di lettura)
        def _ci(p) -> int:
            return (p.payload or {}).get("chunk_index", 0)

        points.sort(key=_ci)
        num_chunks = len(points)
        max_idx = _ci(points[-1])  # vero indice massimo presente (gli indici possono avere buchi)
        filename = (points[0].payload or {}).get("filename")
        cap = max(0, int(max_chars))

        # finestra opzionale per chunk_index [from_chunk, to_chunk) — per leggere la CODA
        # di documenti lunghi senza rilanciare alla cieca con max_chars sempre più alto.
        window = points
        if from_chunk is not None or to_chunk is not None:
            lo = int(from_chunk) if from_chunk is not None else None
            hi = int(to_chunk) if to_chunk is not None else None
            window = [
                p for p in points
                if (lo is None or _ci(p) >= lo) and (hi is None or _ci(p) < hi)
            ]

        if not window:
            # finestra oltre la fine o caduta in un buco di indici: niente da restituire e
            # NESSUNA coda oltre questo punto → has_more=False (non spingere l'agent in un loop).
            return {
                "file_id": file_id, "filename": filename, "num_chunks": num_chunks,
                "chunk_range": {"from": None, "to": None},
                "truncated": False, "has_more": False, "text": "",
            }

        # Accumula chunk INTERI finché aggiungerne uno sforerebbe max_chars: così il testo finisce
        # SEMPRE a un confine di chunk e `from_chunk = chunk_range.to + 1` riprende esattamente da
        # dove ci si è fermati, senza perdere la coda (il bug del troncamento a metà-finestra).
        sep = "\n\n"
        emitted, total = [], 0
        for p in window:
            t = (p.payload or {}).get("text", "")
            add = (len(sep) if emitted else 0) + len(t)
            if emitted and total + add > cap:
                break
            emitted.append(p)
            total += add
        if not emitted:
            emitted = [window[0]]  # singolo chunk > max_chars: emettilo comunque (no loop a vuoto)

        text = sep.join((p.payload or {}).get("text", "") for p in emitted)
        truncated = len(text) > cap
        if truncated:
            text = text[:cap]
        last_emitted = _ci(emitted[-1])
        return {
            "file_id": file_id,
            "filename": filename,
            "num_chunks": num_chunks,
            "chunk_range": {"from": _ci(emitted[0]), "to": last_emitted},
            "truncated": truncated,             # True solo se un singolo chunk supera max_chars
            "has_more": last_emitted < max_idx, # se True: richiama con from_chunk = chunk_range.to + 1
            "text": text,
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        return _err(e, file_id=file_id, text="")


# --------------------------------------------------------------------------- #
# NLP (stesse istanze del router /v1/nlp — una copia sola dei modelli)         #
# --------------------------------------------------------------------------- #

async def _core_extract_entities(text: str, labels: Optional[List[str]] = None) -> Dict[str, Any]:
    from routers.nlp import _ner

    if await asyncio.to_thread(_ner.load) is None:
        return {"error": "GLiNER non disponibile su questo deployment", "entities": []}
    results = await asyncio.to_thread(_ner.extract, [text], labels)
    return {"entities": results[0] if results else []}


async def _core_classify_text(
    text: str, labels: Optional[List[str]] = None, threshold: Optional[float] = None
) -> Dict[str, Any]:
    from routers.nlp import _classifier

    if await asyncio.to_thread(_classifier.load) is None:
        return {"error": "Classifier (GliClass) non disponibile", "classes": []}
    results = await asyncio.to_thread(_classifier.classify, [text], labels, threshold)
    return {"classes": results[0] if results else []}


async def _core_extract_relations(
    text: str,
    entity_labels: Optional[List[str]] = None,
    relation_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from routers.nlp import _relex

    if await asyncio.to_thread(_relex.load) is None:
        return {"error": "GLiNER-relex non disponibile su questo deployment", "relations": []}
    results = await asyncio.to_thread(_relex.extract, [text], entity_labels, relation_labels)
    return {"relations": results[0] if results else []}


# =========================================================================== #
# Descrizioni dei tool (varianti globale / scoped)                            #
# =========================================================================== #

_VS_HINT = "Passa `vector_store_id` (vedi list_vector_stores). "
_SCOPED_NOTE = "Il vector store è già fissato (sei instradato su uno store specifico)."

SEARCH_DESC = (
    "Ricerca semantica (hybrid dense+sparse + rerank) sui documenti di un vector store. "
    "È il tool RAG. " + _VS_HINT + "Passa la `query` in linguaggio naturale, **un concetto per "
    "query**. Per approfondire un documento già individuato passa `filename` (drill-in: più "
    "efficace di una nuova query ampia); `directory_slug` restringe a una cartella. `graph_expand="
    "true` espande coi documenti collegati nel knowledge graph. `score_threshold` (0–1) rende la "
    "ricerca CHIRURGICA: alzalo (es. 0.5+) per un controllo esistenza/assenza — risultato vuoto = "
    "non nel corpus, non query mal posta (default backend 0.25, tarato su gold-set). Ogni risultato "
    "porta testo, file/score, e provenienza citabile (pagina, sezione/headings, canale)."
)
SEARCH_DESC_SCOPED = SEARCH_DESC.replace(_VS_HINT, _SCOPED_NOTE + " ")

OVERVIEW_DESC = (
    "Quadro d'insieme di un vector store da leggere PRIMA di cercare, per capire di cosa parla e "
    "com'è organizzato: conteggi (punti/file per stato), distribuzione per directory e per "
    "categoria, `topics` (temi trasversali via clustering, con i documenti più rappresentativi) e "
    "stat del knowledge graph. " + _VS_HINT + "È cachato → economico. È il passo di orientamento "
    "del workflow rag_research."
)
OVERVIEW_DESC_SCOPED = (
    "Quadro d'insieme dello store da leggere PRIMA di cercare: conteggi, distribuzione per "
    "directory e categoria, `topics` (temi trasversali coi documenti rappresentativi) e stat del "
    "knowledge graph. " + _SCOPED_NOTE + " È cachato → economico. Passo di orientamento del workflow."
)

LIST_VS_DESC = (
    "Elenca i vector store disponibili (le collezioni indicizzate). Usalo prima di search per "
    "scoprire su quale `vector_store_id` cercare. Ritorna id, nome e conteggi file."
)

LIST_DIRS_DESC = (
    "Elenca le directory (raggruppamenti logici di documenti) con id/name/slug/file_count e le "
    "`properties` (metadati di dominio filtrabili). Opzionale `vector_store_id` per filtrare a uno "
    "store. Utile per capire come è organizzato il corpus."
)
LIST_DIRS_DESC_SCOPED = (
    "Elenca le directory di questo store con id/name/slug/file_count e le `properties` (metadati di "
    "dominio filtrabili). " + _SCOPED_NOTE + " Utile per capire come è organizzato il corpus."
)

LIST_FILES_DESC = (
    "Catalogo dei documenti di un vector store (a lista). `name_contains` filtra per sottostringa "
    "del nome file (case-insensitive). Orientarsi col catalogo PRIMA di cercare rende la search più "
    "mirata. " + _VS_HINT + "Ritorna nome, file_id, n. chunk, stato, eventuale `error` (se il file è "
    "FAILED) e gli `attributes` (slug/proprietà della directory)."
)
LIST_FILES_DESC_SCOPED = LIST_FILES_DESC.replace(_VS_HINT, _SCOPED_NOTE + " ")

SEARCH_BY_NAME_DESC = (
    "Trova documenti per nome (sottostringa, case-insensitive). Più veloce della ricerca semantica "
    "quando conosci il nome o un codice del file. " + _VS_HINT + "Ritorna i file corrispondenti con "
    "file_id, n. chunk, stato ed eventuale error; poi usa get_document o search(filename=…)."
)
SEARCH_BY_NAME_DESC_SCOPED = SEARCH_BY_NAME_DESC.replace(_VS_HINT, _SCOPED_NOTE + " ")

GET_DOC_DESC = (
    "Testo di un documento, ricostruito dai suoi chunk in ordine di lettura. Per il deep-dive "
    "quando i chunk della search sono pertinenti ma parziali. " + _VS_HINT + "Passa il `file_id`. "
    "`max_chars` tronca l'output (default 20000). Per documenti lunghi usa la finestra `from_chunk`/"
    "`to_chunk`: se `has_more=true` c'è altra coda → richiama con `from_chunk` = chunk_range.to + 1."
)
GET_DOC_DESC_SCOPED = GET_DOC_DESC.replace(_VS_HINT, _SCOPED_NOTE + " ")

NER_DESC = (
    "Estrazione entità zero-shot (GLiNER) + regex ad alta precisione (IBAN, importi, riferimenti "
    "normativi…). `labels` opzionale (lista di tipi entità) altrimenti usa lo schema di default. "
    "Ritorna le entità trovate nel testo."
)
CLS_DESC = (
    "Classificazione zero-shot del testo (GliClass) su label arbitrarie. `labels` opzionale "
    "altrimenti usa quelle di default. Ritorna [{label, score}] sopra soglia."
)
REL_DESC = (
    "Estrazione di relazioni tipizzate zero-shot (GLiNER-relex): triple (head, relazione, tail) tra "
    "le entità del testo. `entity_labels`/`relation_labels` opzionali. Utile per il knowledge graph."
)


# =========================================================================== #
# Tabella unica + registrazione su entrambe le superfici                       #
# =========================================================================== #

# (core, name, scope, desc_global, desc_scoped)
#   scope="store"    → su /mcp con vector_store_id esplicito; su scoped vs preso dal path
#   scope="global"   → solo su /mcp (non ha senso quando lo store è fissato)
#   scope="agnostic" → identico su entrambe (non dipende da un vector store)
_TOOLS = [
    (_core_search,            "search",             "store",    SEARCH_DESC,         SEARCH_DESC_SCOPED),
    (_core_corpus_overview,   "corpus_overview",    "store",    OVERVIEW_DESC,       OVERVIEW_DESC_SCOPED),
    (_core_list_files,        "list_files",         "store",    LIST_FILES_DESC,     LIST_FILES_DESC_SCOPED),
    (_core_search_by_name,    "search_by_name",     "store",    SEARCH_BY_NAME_DESC, SEARCH_BY_NAME_DESC_SCOPED),
    (_core_get_document,      "get_document",       "store",    GET_DOC_DESC,        GET_DOC_DESC_SCOPED),
    (_core_list_directories,  "list_directories",   "store",    LIST_DIRS_DESC,      LIST_DIRS_DESC_SCOPED),
    (_core_list_vector_stores,"list_vector_stores", "global",   LIST_VS_DESC,        None),
    (_core_extract_entities,  "extract_entities",   "agnostic", NER_DESC,            NER_DESC),
    (_core_classify_text,     "classify_text",      "agnostic", CLS_DESC,            CLS_DESC),
    (_core_extract_relations, "extract_relations",  "agnostic", REL_DESC,            REL_DESC),
]


def _make_scoped(core):
    """Genera la variante 'scoped' di un core store-scoped: stessa logica, ma con uno schema
    SENZA `vector_store_id` (l'agent non lo vede). Il vs è preso dal path del mount
    /v1/vector_stores/{vsid}/mcp via `ctx.request_context.request.path_params['vsid']`.

    NB: stateless_http=True fa girare il tool in un task separato da quello ASGI della request,
    quindi un ContextVar settato da un middleware NON propagherebbe → si legge dal `ctx`."""
    sig = inspect.signature(core)
    params = [p for n, p in sig.parameters.items() if n != "vector_store_id"]
    # ctx: Context → FastMCP lo riconosce, lo inietta e lo ESCLUDE dallo schema esposto.
    params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))
    new_sig = sig.replace(parameters=params)

    @functools.wraps(core)
    async def wrapper(*args, **kwargs):
        ctx = kwargs.pop("ctx", None)
        try:
            vsid = ctx.request_context.request.path_params.get("vsid")
        except Exception:
            vsid = None
        if not vsid:
            return {"error": "vector store non determinato dal path", "status": "backend_error"}
        return await core(vsid, *args, **kwargs)

    wrapper.__signature__ = new_sig  # ha precedenza su __wrapped__ in inspect.signature
    ann = {n: p.annotation for n, p in new_sig.parameters.items() if p.annotation is not inspect._empty}
    if sig.return_annotation is not inspect._empty:
        ann["return"] = sig.return_annotation
    wrapper.__annotations__ = ann
    wrapper.__name__ = core.__name__.replace("_core_", "")
    return wrapper


def _register(mcp_global: FastMCP, mcp_store: FastMCP) -> None:
    """Registra ogni tool della tabella su entrambe le superfici, una volta sola."""
    for core, name, scope, desc_global, desc_scoped in _TOOLS:
        # il title JSON-Schema (input/output) è derivato da fn.__name__: senza questo trapelerebbe
        # `_core_searchArguments` sul wire della superficie globale. Il nome pubblico È `name`.
        core.__name__ = name
        mcp_global.add_tool(core, name=name, description=desc_global, annotations=_RO)
        if scope == "agnostic":
            mcp_store.add_tool(core, name=name, description=desc_scoped, annotations=_RO)
        elif scope == "store":
            mcp_store.add_tool(_make_scoped(core), name=name, description=desc_scoped, annotations=_RO)
        # scope == "global": non esposto sulla superficie scoped


# streamable_http_path="/" — l'handler interno sta su "/", non sul default "/mcp": montandolo
# su "/mcp" (o sul path scoped) in main.py l'endpoint finale è quello del mount, non /…/mcp/mcp.
mcp = FastMCP("Sophia Vector", stateless_http=True, json_response=True, streamable_http_path="/")
mcp_scoped = FastMCP("Sophia Vector (store)", stateless_http=True, json_response=True, streamable_http_path="/")

_register(mcp, mcp_scoped)


# =========================================================================== #
# Prompt — workflow agnostico riutilizzabile ("skill" portabile via MCP)       #
# =========================================================================== #

def _rag_research_text(question: str, scoped: bool, vector_store_id: str = "") -> str:
    """Template agnostico: nessun assunto di dominio, solo il metodo di ricerca efficace."""
    if scoped:
        target = "in questa knowledge base (lo store è già fissato)"
        discover = "`corpus_overview` (mappa di temi/categorie) e `list_files`/`list_directories`"
    elif vector_store_id:
        target = f"nel vector store `{vector_store_id}`"
        discover = "`corpus_overview` (mappa di temi/categorie) e `list_files`/`list_directories`"
    else:
        target = "scegliendo il vector store con list_vector_stores"
        discover = "`corpus_overview` (mappa di temi/categorie) e `list_files`/`list_directories`"

    return f"""Rispondi a questa domanda interrogando la knowledge base {target}, usando i tool MCP di Sophia Vector. Segui questo metodo:

DOMANDA: {question}

1. ORIENTAMENTO (quasi gratis): parti da {discover} per capire di cosa parla il corpus e QUALI documenti esistono sul tema PRIMA di cercare. Se conosci un nome/codice preciso, `search_by_name`.

2. RICERCA semantica con `search`: una query = UN concetto, 4–8 parole, in una sola lingua. Niente date/nomi propri nella query (per quelli filtra). Valuta gli score: >0.65 molto rilevante, 0.55–0.65 rilevante, <0.45 ignora. Per un controllo esistenza/assenza alza `score_threshold` (es. 0.5): vuoto = non nel corpus. Tieniti un budget basso (≈4 ricerche): se non basta, riformula con sinonimi o sotto-domande invece di ripetere.

3. DRILL-IN: quando un documento è rilevante ma i chunk sono parziali, NON lanciare nuove query ampie — usa `search(filename=…)` per cercare dentro quel file, o `get_document(file_id=…)` per leggerne il testo (se `has_more=true`, continua con `from_chunk`).

4. SINTESI: rispondi USANDO SOLO ciò che hai trovato. Cita sempre la fonte (il `filename`, e se presente pagina/sezione dai metadati dei risultati). Non inventare, non riempire i vuoti con conoscenza generica, non citare documenti che non compaiono nei risultati. Se dopo il budget non hai abbastanza, dillo e indica cosa hai provato."""


@mcp.prompt(
    title="RAG research workflow",
    description="Workflow per rispondere a una domanda interrogando un vector store, citando le fonti.",
)
def rag_research(question: str, vector_store_id: str = "") -> str:
    return _rag_research_text(question, scoped=False, vector_store_id=vector_store_id)


@mcp_scoped.prompt(  # noqa: F811 — stesso nome di prompt, superficie diversa (store fisso dal path)
    title="RAG research workflow",
    description="Workflow per rispondere a una domanda interrogando questa knowledge base, citando le fonti.",
)
def rag_research(question: str) -> str:  # noqa: F811
    return _rag_research_text(question, scoped=True)
