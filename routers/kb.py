"""KB surface — i tool dell'MCP esposti via REST, con access control per-directory
OBBLIGATORIO.

Pensata per un consumer (es. un agent) che NON può usare il trasporto MCP: passa le
directory consentite (dalla sessione/utente) a OGNI chiamata e il backend le impone
come must-filter Qdrant. È un **thin layer**: nessuna logica di retrieval qui, riusa
gli STESSI `_core_*` dei tool MCP (`routers/mcp.py`) → una sola fonte di verità.

Differenza dalle superfici admin (`/search`, `/files`, …): qui `directories` è
**richiesto** (422 se manca) → l'isolamento non è bypassabile per costruzione. Tutti
gli endpoint sono read-only e protetti dalla stessa API key di `/v1/*`.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from routers.mcp import (
    _core_search,
    _core_corpus_overview,
    _core_list_files,
    _core_search_by_name,
    _core_get_document,
    _core_list_directories,
)

router = APIRouter(prefix="/v1/vector_stores", tags=["KB (agent)"])


def _parse_dirs(directories: Optional[str]) -> List[str]:
    """CSV di slug → lista non vuota. 422 se manca (access control obbligatorio)."""
    dirs = [d.strip() for d in (directories or "").split(",") if d.strip()]
    if not dirs:
        raise HTTPException(
            status_code=422,
            detail="parametro 'directories' obbligatorio (CSV degli slug consentiti)",
        )
    return dirs


class KbSearch(BaseModel):
    query: str
    directories: List[str]                      # access control — obbligatorio
    max_results: int = 8
    filename: Optional[str] = None              # drill-in opzionale dentro un file
    directory_slug: Optional[str] = None        # restringe a UNA directory (oltre allo scope)
    graph_expand: bool = False
    score_threshold: Optional[float] = None


@router.post("/{vector_store_id}/kb/search")
async def kb_search(vector_store_id: str, body: KbSearch) -> Dict[str, Any]:
    """Ricerca semantica (hybrid + rerank + canali lessicale/grafo) ristretta alle
    `directories` consentite. Stessa pipeline di `/search`, con scope obbligatorio."""
    dirs = [d for d in (body.directories or []) if d]
    if not dirs:
        raise HTTPException(status_code=422, detail="'directories' obbligatorio (non vuoto)")
    return await _core_search(
        vector_store_id,
        query=body.query,
        max_results=body.max_results,
        filename=body.filename,
        directory_slug=body.directory_slug,
        graph_expand=body.graph_expand,
        score_threshold=body.score_threshold,
        directories=dirs,
    )


@router.get("/{vector_store_id}/kb/files")
async def kb_files(
    vector_store_id: str,
    directories: str = Query(..., description="CSV degli slug consentiti"),
    name: Optional[str] = Query(None, description="filtra per sottostringa del nome file"),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """Catalogo dei documenti (da Mongo, no scroll) ristretto alle directory consentite."""
    return await _core_list_files(
        vector_store_id, name_contains=name, limit=limit, directories=_parse_dirs(directories)
    )


@router.get("/{vector_store_id}/kb/search_by_name")
async def kb_search_by_name(
    vector_store_id: str,
    name: str = Query(..., description="sottostringa del nome file"),
    directories: str = Query(..., description="CSV degli slug consentiti"),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Trova documenti per nome nelle directory consentite (più veloce della semantica)."""
    return await _core_search_by_name(
        vector_store_id, name=name, limit=limit, directories=_parse_dirs(directories)
    )


@router.get("/{vector_store_id}/kb/document/{file_id}")
async def kb_document(
    vector_store_id: str,
    file_id: str,
    directories: str = Query(..., description="CSV degli slug consentiti"),
    max_chars: int = Query(20000, ge=1),
    from_chunk: Optional[int] = Query(None),
    to_chunk: Optional[int] = Query(None),
) -> Dict[str, Any]:
    """Testo pieno di un documento (paginabile per chunk). Un file_id fuori dalle
    directory consentite ritorna not_found (niente leak)."""
    return await _core_get_document(
        vector_store_id, file_id, max_chars=max_chars,
        from_chunk=from_chunk, to_chunk=to_chunk, directories=_parse_dirs(directories),
    )


@router.get("/{vector_store_id}/kb/directories")
async def kb_directories(
    vector_store_id: str,
    directories: str = Query(..., description="CSV degli slug consentiti"),
) -> Dict[str, Any]:
    """Elenca SOLO le directory consentite di questo store (con file_count/properties)."""
    return await _core_list_directories(vector_store_id, directories=_parse_dirs(directories))


@router.get("/{vector_store_id}/kb/overview")
async def kb_overview(
    vector_store_id: str,
    directories: str = Query(..., description="CSV degli slug consentiti"),
) -> Dict[str, Any]:
    """Orientamento ("navigate"): quadro d'insieme delle SOLE directory consentite —
    cartelle + conteggi (file/punti). Da leggere PRIMA di cercare. Vista sicura: niente
    cluster/temi store-wide (eviterebbero il leak cross-directory)."""
    return await _core_corpus_overview(vector_store_id, directories=_parse_dirs(directories))
