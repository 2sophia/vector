"""Astrazione provider per le ingestion sources.

Una "source" è una connessione esterna da cui ingerire documenti (SharePoint,
Google Drive, S3, ...). Ogni provider dichiara:
- i campi di configurazione (per il form dinamico in UI e la validazione)
- quali campi sono secret (cifrati at-rest)
- come navigare (browse) la sorgente

La pipeline a valle (download → ingestion_jobs → vector worker → Qdrant) è
GIÀ agnostica: un nuovo provider implementa solo auth/browse/download.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ProviderField(BaseModel):
    """Un campo di configurazione di una source (guida il form UI + validazione)."""
    name: str
    label: str
    type: str = "text"          # "text" | "password"
    placeholder: str = ""
    required: bool = True
    secret: bool = False        # se True viene cifrato at-rest (campo `<name>_enc`)


class SourceProvider:
    """Classe base di un provider. Le sottoclassi impostano gli attributi e,
    se `enabled`, implementano `browse` (e in futuro download/auth)."""

    type: str = ""
    label: str = ""
    enabled: bool = False
    config_fields: List[ProviderField] = []

    def secret_fields(self) -> List[str]:
        return [f.name for f in self.config_fields if f.secret]

    def describe(self) -> Dict[str, Any]:
        """Descrizione pubblica per la UI (/v1/sources/types)."""
        return {
            "type": self.type,
            "label": self.label,
            "enabled": self.enabled,
            "config_fields": [f.model_dump() for f in self.config_fields],
        }

    def browse(
        self,
        config: Dict[str, Any],
        drive_id: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Naviga la sorgente. Ritorna {level, drives|folders, ...}."""
        raise NotImplementedError(f"browse non disponibile per il provider '{self.type}'")
