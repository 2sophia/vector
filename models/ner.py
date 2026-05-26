"""
NerModel — estrazione entità (M3 knowledge graph).

GLiNER zero-shot per le entità "soft" (organizzazione, persona, normativa, data,
importo, luogo…) + regex per quelle strutturate del dominio bancario (IBAN, codice
fiscale, P.IVA), dove un pattern è più preciso e robusto di un modello. Niente LLM.

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


_REGEX_RULES = [
    ("iban", re.compile(r"\bIT\d{2}[A-Z]\d{22}\b", re.IGNORECASE), _norm_compact),
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
                out.append({
                    "name": raw.strip(),
                    "type": etype,
                    "normalized_name": normalizer(raw),
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
                        name = e["text"].strip()
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
