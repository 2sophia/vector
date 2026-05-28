"""API Routers"""

from .vector_stores import router as vector_stores_router
from .files import router as files_router
from .search import router as search_router
from .sharepoint import router as sharepoint_router
from .sources import router as sources_router
from .directories import router as directories_router
from .schedules import router as schedules_router
from .nlp import router as nlp_router

__all__ = [
    'sharepoint_router',
    'vector_stores_router',
    'files_router',
    'search_router',
    'sources_router',
    'directories_router',
    'schedules_router',
    'nlp_router',
]
