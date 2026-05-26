"""
NerModel — estrazione entità (M3 knowledge graph).

GLiNER zero-shot per le entità "soft" (organizzazione, persona, normativa, data,
importo, luogo…) + regex per quelle strutturate, dove un pattern è più preciso e
robusto di un modello. La regex copre due famiglie: UNIVERSALI/agnostiche (email,
URL, IBAN, ISIN, importo, percentuale, data) e IT-banking (codice fiscale, P.IVA).
Niente LLM.

Il modello è **config** (GLINER_MODEL): la classe è agnostica → si benchmarka un
altro GLiNER cambiando l'env. GLiNER tronca a max_len token (≈384 per mdeberta-base),
quindi i chunk lunghi vengono processati a **finestre** con overlap; le finestre
vanno a `model.inference` in **mini-batch** (vedi ModelBase._batched) per non far
esplodere la VRAM. Output per entità: {name, type, normalized_name, score}.
"""

import re
from typing import Any, Dict, List

from utils.logger import get_logger
from utils.settings import GLINER_THRESHOLD, GLINER_LABELS
from .base import ModelBase
from .text import normalize

logger = get_logger(__name__)


# --- Regex per entità strutturate (auto-identificanti, basso falso-positivo) ---
def _norm_compact(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def _lower(s: str) -> str:
    return s.strip().lower()


def _norm_money(s: str) -> str:
    # canonicalizza "€ 1.250,00" e "1.250,00 EUR" alla stessa forma (solo numero)
    return re.sub(r"(?:€|eur|euro)", "", s, flags=re.IGNORECASE).strip()


def _norm_ref(s: str) -> str:
    # canonicalizza i riferimenti normativi così che le varianti collassino sullo
    # stesso nodo: "D.Lgs. n. 385/1993"="DLgs 385/1993", "Regolamento UE 2016/679"=
    # "Regolamento (UE) 2016/679", "Circolare Banca d'Italia n. 285"="Circolare 285".
    s = re.sub(r"\bn\.?", "", s, flags=re.IGNORECASE)                 # "n."
    s = re.sub(r"\(?\b(?:UE|CE)\b\)?", "", s, flags=re.IGNORECASE)    # UE/CE/(UE)
    s = re.sub(r"banca\s*d['’]italia", "", s, flags=re.IGNORECASE)    # emittente
    s = re.sub(r"\b(?:del|della|dello|dei|degli|delle)\b", "", s, flags=re.IGNORECASE)
    return re.sub(r"[.\s()'’]+", "", s).upper()


# Famiglia A — UNIVERSALI (agnostiche, valgono per qualsiasi dominio). Pattern ad
# alta precisione: meglio di una label zero-shot per ciò che è auto-identificante.
# Famiglia "IT-banking" — identificatori strutturati italiani.
# NB sui falsi positivi: le date le prendiamo SOLO con slash o in ISO, MAI con i
# punti (es. "1.6.3" è un numero di sezione, non una data); gli importi solo con
# valuta esplicita; la P.IVA solo con contesto.
_REGEX_RULES = [
    # --- famiglia A: universali ---
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), _lower),
    ("url", re.compile(r"(?:https?://|(?<![@\w])www\.)[^\s<>\")]*[^\s<>\").,;:!?]", re.IGNORECASE), _lower),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), _norm_compact),
    ("isin", re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b"), _norm_compact),
    ("importo monetario", re.compile(
        r"(?:€|EUR|euro)\s?\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?"
        r"|\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?\s?(?:€|EUR|euro)\b", re.IGNORECASE), _norm_money),
    ("percentuale", re.compile(r"\b\d{1,3}(?:[.,]\d+)?\s?%"), _norm_compact),
    ("data", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"), _norm_compact),
    # --- famiglia B: riferimenti normativi (tira sul dominio banking/legal, alta
    # precisione: numero/anno obbligatorio per decreti/leggi/regolamenti → niente
    # falsi positivi su "art. 3" o "comma 5" sciolti) ---
    ("normativa", re.compile(
        r"\b(?:D\.?\s?Lgs\.?|D\.?\s?M\.?|D\.?\s?P\.?R\.?|D\.?\s?L\.?"
        r"|Decreto\s+Legislativo|Decreto\s+Ministeriale)\s*(?:n\.?\s*)?\d+/\d{2,4}", re.IGNORECASE), _norm_ref),
    ("normativa", re.compile(r"\b(?:Legge|L\.)\s*(?:n\.?\s*)?\d+/\d{2,4}", re.IGNORECASE), _norm_ref),
    ("normativa", re.compile(
        r"\bReg(?:olamento|\.)\s*(?:\(?(?:UE|CE)\)?\s*)?(?:n\.?\s*)?\d+/\d{2,4}", re.IGNORECASE), _norm_ref),
    ("normativa", re.compile(
        r"\bCircolare\s*(?:Banca\s+d['’]Italia\s*)?(?:n\.?\s*)?\d+", re.IGNORECASE), _norm_ref),
    ("normativa", re.compile(
        r"\bart(?:icolo|\.)?\s*\d+(?:\s*(?:bis|ter|quater|quinquies))?(?:\s*,?\s*comma\s*\d+)?"
        r"\s*(?:del|della)?\s*(?:T\.?U\.?[BFC]?|TUB|TUF|TUC|c\.?c\.?|cod(?:\.|ice)?\s*civ(?:\.|ile)?)\b",
        re.IGNORECASE), _norm_ref),
    # --- famiglia IT-banking: identificatori strutturati italiani ---
    ("codice fiscale", re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b"), _norm_compact),
    # P.IVA solo con contesto esplicito, per evitare falsi positivi su numeri da 11 cifre
    ("partita iva", re.compile(r"(?:partita\s+iva|p\.?\s*iva)[\s:.]*?(\d{11})", re.IGNORECASE), _norm_compact),
]


class NerModel(ModelBase):
    # Quante finestre per chiamata di inference (limita il picco VRAM su GPU).
    INFER_BATCH = 16
    # Finestre in parole (≈ token) con overlap per non spezzare entità sul confine.
    WINDOW_WORDS = 300
    WINDOW_OVERLAP = 30

    def _windows(self, text: str) -> List[str]:
        words = text.split()
        if len(words) <= self.WINDOW_WORDS:
            return [text]
        out: List[str] = []
        step = self.WINDOW_WORDS - self.WINDOW_OVERLAP
        for start in range(0, len(words), step):
            out.append(" ".join(words[start:start + self.WINDOW_WORDS]))
            if start + self.WINDOW_WORDS >= len(words):
                break
        return out

    def _regex_entities(self, text: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for etype, pattern, normalizer in _REGEX_RULES:
            for match in pattern.finditer(text):
                raw = match.group(1) if match.groups() else match.group(0)
                # collassa lo whitespace interno: un match può andare a cavallo di
                # un a-capo (es. "Legge\n201/2011") → "Legge 201/2011"
                name = re.sub(r"\s+", " ", raw).strip()
                out.append({
                    "name": name,
                    "type": etype,
                    "normalized_name": normalizer(name),
                    "score": 1.0,
                })
        return out

    @staticmethod
    def _dedup(ents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Una entità per (type, normalized_name), con lo score massimo."""
        best: Dict[tuple, Dict[str, Any]] = {}
        for e in ents:
            if not e["normalized_name"]:
                continue
            key = (e["type"], e["normalized_name"])
            if key not in best or e["score"] > best[key]["score"]:
                best[key] = e
        return list(best.values())

    def extract(
        self, texts: List[str], labels: List[str] | None = None
    ) -> List[List[Dict[str, Any]]]:
        """Estrae entità per una lista di testi (un chunk per testo). `labels`:
        schema entità per-collection; se None usa GLINER_LABELS. Ritorna una lista
        allineata a `texts`. Best-effort."""
        gliner_labels = labels or GLINER_LABELS
        n = len(texts)
        results: List[List[Dict[str, Any]]] = [[] for _ in range(n)]

        model = self.load()
        if model is not None and texts:
            segments: List[str] = []
            owners: List[int] = []
            for i, text in enumerate(texts):
                for w in self._windows(text):
                    segments.append(w)
                    owners.append(i)
            try:
                batch: List[List[Dict[str, Any]]] = []
                for sub in self._batched(segments, self.INFER_BATCH):
                    batch.extend(model.inference(sub, gliner_labels, threshold=GLINER_THRESHOLD))
                for seg_i, ents in enumerate(batch):
                    owner = owners[seg_i]
                    for e in ents:
                        # collassa whitespace interno: GLiNER può restituire span a
                        # cavallo di un a-capo (es. "Consorzi\nMontante") → "Consorzi Montante"
                        name = re.sub(r"\s+", " ", e["text"]).strip()
                        # scarta entità troppo lunghe: quasi sempre rumore
                        if len(name) > 80 or len(name.split()) > 12:
                            continue
                        results[owner].append({
                            "name": name,
                            "type": e["label"],
                            "normalized_name": normalize(name),
                            "score": float(e.get("score", 0.0)),
                        })
            except Exception as e:
                logger.warning(f"GLiNER inference failed: {e}")
            finally:
                self._empty_cache()

        # Regex (structured entities) sul testo intero, poi dedup
        for i, text in enumerate(texts):
            results[i].extend(self._regex_entities(text))
            results[i] = self._dedup(results[i])
        return results
