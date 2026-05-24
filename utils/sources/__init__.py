"""Registry dei provider di ingestion source."""

from typing import List, Optional

from .base import ProviderField, SourceProvider
from .providers import (
    SharePointProvider,
    GoogleDriveProvider,
    GoogleWorkspaceProvider,
    S3Provider,
)

# Ordine = ordine di visualizzazione in UI. SharePoint primo (enabled).
_PROVIDER_LIST: List[SourceProvider] = [
    SharePointProvider(),
    GoogleDriveProvider(),
    GoogleWorkspaceProvider(),
    S3Provider(),
]

PROVIDERS = {p.type: p for p in _PROVIDER_LIST}


def get_provider(source_type: str) -> Optional[SourceProvider]:
    return PROVIDERS.get(source_type)


def list_providers() -> List[SourceProvider]:
    return _PROVIDER_LIST


__all__ = [
    "ProviderField",
    "SourceProvider",
    "PROVIDERS",
    "get_provider",
    "list_providers",
]
