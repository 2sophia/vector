"""
Autenticazione opzionale degli endpoint /v1/* via API key.

Postura single-tenant: un deploy = un'organizzazione, tutti gli operatori vedono
gli stessi dati (by design). Il backend NON isola per utente. La protezione del
backend è quindi a livello di rete + un'unica API key condivisa:

- SOPHIA_VECTOR_API_KEY vuota (default) → nessun controllo. Adatto allo sviluppo e
  ai deploy in cui il backend è raggiungibile solo dal frontend/rete fidata.
- SOPHIA_VECTOR_API_KEY valorizzata → gli endpoint /v1/* richiedono
  `Authorization: Bearer <key>`. Il proxy del frontend inoltra la key (env
  BACKEND_API_KEY, da tenere allineata). Da impostare se la porta del backend è
  esposta oltre la rete fidata.
"""

import hmac

from fastapi import Header, HTTPException

from utils.config import settings


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """Dependency: applica il check Bearer solo se SOPHIA_VECTOR_API_KEY è impostata."""
    expected = settings.API_KEY
    if not expected:
        return  # key non configurata → nessun controllo (postura di default)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization[len("Bearer "):].strip()
    # confronto a tempo costante: non rivela la lunghezza/prefisso della key via timing
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid API key")
