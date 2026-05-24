"""
Cifratura simmetrica per i secret delle ingestion sources (es. il
client_secret SharePoint), così non finiscono mai in chiaro su MongoDB.

Usa Fernet (AES-128-CBC + HMAC). La chiave deriva da SOPHIA_VECTOR_SECRET_KEY
via SHA-256: accetta una passphrase arbitraria e la normalizza a una chiave
Fernet valida (32 byte url-safe base64).

In assenza di SECRET_KEY si usa una chiave di sviluppo NON sicura, con
warning: va impostata in produzione, altrimenti i secret sono decifrabili
da chiunque conosca il fallback.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet

from utils.config import settings

logger = logging.getLogger(__name__)

_DEV_FALLBACK_KEY = "sophia-vector-dev-insecure-key"
_warned = False


def _get_fernet() -> Fernet:
    global _warned
    key = settings.SECRET_KEY
    if not key:
        if not _warned:
            logger.warning(
                "SOPHIA_VECTOR_SECRET_KEY non impostata: uso una chiave di sviluppo "
                "NON sicura. Impostala in produzione per proteggere i secret delle source."
            )
            _warned = True
        key = _DEV_FALLBACK_KEY
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Cifra una stringa. Stringa vuota → stringa vuota."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decifra una stringa prodotta da encrypt_secret. Vuota → vuota."""
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
