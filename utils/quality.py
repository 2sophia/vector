"""
Quality scoring euristico dei chunk — segnale ADDITIVO, zero modelli, zero rete.

Tesi (cascata di data-curation, vedi `data-curation-dedup-sota.md`): prima di
spendere su modelli pesanti, la maggior parte della spazzatura testuale (gibberish,
artefatti OCR, frammenti vuoti, testo super-ripetitivo) si becca con euristiche
deterministiche L1/L2 a costo ~zero. Qui calcoliamo un `quality_score` ∈ [0,1]
(1 = buono) per ogni chunk, da salvare nel payload Qdrant.

Filosofia "solo migliorativo":
  - NON scarta nulla a ingestion: lo score è un metadato in più, si indicizza
    sempre. Il filtro è OPT-IN a search-time (`min_quality`, default 0 = no-op).
  - best-effort REALE: qualunque eccezione qui ritorna score neutro (1.0) e non
    deve MAI far fallire un job (chiamato in un try difensivo dal worker).
  - agnostico: nessun dominio hardcoded, nessuna lingua privilegiata oltre alle
    vocali latine (il proxy gibberish vale per IT/EN/lingue latine).

Le soglie sono CONSERVATIVE: prosa normale (IT/EN) prende ~0.9–1.0, solo il testo
genuinamente rotto scende. I chunk tabulari (`utils/tabular.py`, righe verbalizzate
con `;`/`:`) restano alti: le penalità simbolo/cifra hanno soglie alte apposta per
non punirli.
"""

import re
from typing import Dict, List, Optional, Sequence, Any

from utils.settings import QUALITY_MIN_WORDS

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_TRIGRAM_MIN_WORDS = 20  # sotto questa soglia la diversità lessicale non è significativa

# Vocali latine incl. accentate (IT) e qualche estesa comune — il proxy gibberish
# misura la frazione di vocali tra le lettere: un testo quasi senza vocali è quasi
# sempre rumore (OCR rotto, base64, hash, tabelle di codici senza parole).
_VOWELS = set("aeiouAEIOUàèéìòùáéíóúâêîôûäëïöüãõyY")


def _ratio(num: int, den: int) -> float:
    return (num / den) if den else 0.0


def _signals(text: str) -> Dict[str, Any]:
    """Estrae i segnali grezzi dal testo (tutti O(n), nessuna allocazione pesante)."""
    n_chars = len(text)
    words = _WORD_RE.findall(text)
    n_words = len(words)

    alpha = sum(1 for c in text if c.isalpha())
    vowels = sum(1 for c in text if c in _VOWELS)
    symbols = sum(1 for c in text if not c.isalnum() and not c.isspace())

    # parola "word-like" misurata SOLO sui token alfabetici: ha almeno una vocale e
    # lunghezza ragionevole. I token puramente numerici (codici, importi, date) sono
    # DATI legittimi, non non-parole → esclusi dal denominatore così le righe
    # tabulari/i corpus pieni di numeri non vengono puniti. Frazione bassa tra le
    # parole alfabetiche = sigle/hash/gibberish invece di linguaggio.
    alpha_words = [w for w in words if any(ch.isalpha() for ch in w)]
    wordlike = sum(
        1 for w in alpha_words if 2 <= len(w) <= 30 and any(ch in _VOWELS for ch in w)
    )

    lower = [w.lower() for w in words]
    unique_ratio = _ratio(len(set(lower)), n_words)

    return {
        "n_chars": n_chars,
        "n_words": n_words,
        "n_alpha_words": len(alpha_words),
        "alpha_ratio": _ratio(alpha, n_chars),
        "vowel_ratio_in_alpha": _ratio(vowels, alpha),
        "symbol_ratio": _ratio(symbols, n_chars),
        "wordlike_ratio": _ratio(wordlike, len(alpha_words)),
        "unique_ratio": unique_ratio,
    }


def score_chunk(text: str, headings: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Ritorna `{quality_score: float∈[0,1], quality_flags: [str]}` per un chunk.

    Lo score parte da 1.0 e sottrae penalità conservative per ogni segnale di
    bassa qualità. I flag spiegano PERCHÉ (diagnostica trasparente, come SORE/NeMo
    raccomandano). Best-effort: su input vuoto/anomalo ritorna score neutro.
    """
    try:
        if not text or not text.strip():
            return {"quality_score": 0.0, "quality_flags": ["empty"]}

        s = _signals(text)
        flags: List[str] = []
        penalty = 0.0

        # L2 — gibberish / OCR-junk: pochissime vocali tra le lettere è il segnale
        # più affidabile di rumore. Soglia bassa (0.15) → quasi solo veri junk.
        if s["alpha_ratio"] > 0.2 and s["vowel_ratio_in_alpha"] < 0.15:
            penalty += 0.5
            flags.append("gibberish")

        # La maggior parte delle PAROLE alfabetiche non sembra linguaggio
        # (sigle/hash/gibberish). Valutato solo se ci sono abbastanza parole alfabetiche
        # (un chunk di soli numeri non è "non_word", è una tabella).
        if s["n_alpha_words"] >= 3 and s["wordlike_ratio"] < 0.5:
            penalty += 0.3
            flags.append("non_word")

        # Frammento troppo corto = poco contenuto informativo (gentile: un heading
        # legittimo è corto ma non è "garbage").
        if s["n_words"] < QUALITY_MIN_WORDS:
            penalty += 0.15
            flags.append("short")

        # Simbolo-pesante: soglia ALTA (0.5) per non punire le righe tabulari
        # verbalizzate (`col: val; ...`) che restano sotto.
        if s["symbol_ratio"] > 0.5:
            penalty += 0.2
            flags.append("symbol_heavy")

        # Testo lungo ma super-ripetitivo (poche parole distinte) = boilerplate/loop.
        if s["n_words"] >= _TRIGRAM_MIN_WORDS and s["unique_ratio"] < 0.25:
            penalty += 0.2
            flags.append("repetitive")

        score = max(0.0, min(1.0, 1.0 - penalty))
        return {"quality_score": round(score, 3), "quality_flags": flags}
    except Exception:
        # Mai rompere l'ingest per un calcolo di qualità.
        return {"quality_score": 1.0, "quality_flags": []}
