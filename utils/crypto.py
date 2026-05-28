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

from cryptography.fernet import Fernet, InvalidToken

from utils.config import settings

logger = logging.getLogger(__name__)

_DEV_FALLBACK_KEY = "sophia-vector-dev-insecure-key"
_warned = False


class SecretDecryptionError(Exception):
    """Il secret cifrato non è decifrabile con la SECRET_KEY corrente (chiave
    cambiata/ruotata o dato corrotto). Il messaggio NON contiene mai il ciphertext."""


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
    """Decifra una stringa prodotta da encrypt_secret. Vuota → vuota.

    Solleva SecretDecryptionError (mai il ciphertext nel messaggio) se il dato non
    è decifrabile: tipicamente la SECRET_KEY è cambiata rispetto a quando il secret
    è stato cifrato. I chiamanti la catturano per chiedere il re-inserimento delle
    credenziali invece di propagare un 500 nudo o mandare il worker in errore ciclico.
    """
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as e:
        logger.warning(
            "decrypt_secret: secret non decifrabile (SECRET_KEY cambiata o dato corrotto): %s",
            type(e).__name__,
        )
        raise SecretDecryptionError(
            "Impossibile decifrare il secret della source: la chiave di cifratura è "
            "cambiata o il dato è corrotto. Re-inserisci le credenziali della source."
        ) from None
