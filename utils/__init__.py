"""Utility functions package"""

# Esporta logger e settings
from .logger import get_logger

from .settings import *

# Esporta funzioni principali
# from .embeddings import get_embeddings
# from .text_extraction import extract_text_from_file
# from .chunking import chunk_text
from .qdrant import build_qdrant_filter, qdrant_client  # Assumo tu abbia questa funzione
from .globals import get_timestamp, generate_id  # Assumo tu abbia questa funzione

__all__ = [
    'get_logger',

    # 'get_embeddings',

    # 'extract_text_from_file',

    # 'chunk_text',

    'build_qdrant_filter',
    'qdrant_client',

    'generate_id',
    'get_timestamp',

    # Esporta anche le settings
    'QDRANT_URL',
    'FILES_STORAGE',
    'MAX_FILE_SIZE',
    'DEFAULT_CHUNK_SIZE',
    'DEFAULT_CHUNK_OVERLAP',
    'DEFAULT_EMBEDDING_DIMENSION',
]
