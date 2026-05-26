"""Normalizzazione testo condivisa tra i modelli (entity resolution).

Estratta da utils/entities perché serve sia alla NER sia al relex: l'id di un
nodo :Entity è `"{type}::{normalized_name}"`, quindi NER e relex DEVONO usare la
stessa normalizzazione per agganciare lo stesso nodo (non crearne di paralleli).
"""

import re


def normalize(name: str) -> str:
    """Lowercase, spazi compattati, punteggiatura di bordo rimossa."""
    return re.sub(r"\s+", " ", name.strip().lower()).strip(" .,;:·•-")
