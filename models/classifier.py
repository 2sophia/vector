"""
ClassifierModel — classificazione zero-shot del testo (GliClass).

A DIFFERENZA di GLiNER (che estrae span = entità), GliClass CLASSIFICA un testo su
label arbitrarie (tipo-documento, tema, sensibilità). Serve a taggare doc/chunk →
payload Qdrant → faceting/filtri nella search. È qui che vivono le categorie
ASTRATTE (concetti AML, processi), che non sono estrazione di span.

Predisposto e OFF di default: attivarlo richiede `pip install gliclass`. Best-effort
come la NER/relex — se la lib o il modello non ci sono, `load()` torna None e
`classify()` torna vuoto (l'ingestion prosegue senza tag). Riusa ModelBase per
device/cache/batching; sovrascrive solo `load()` perché GliClass non è GLiNER.
"""

from typing import Any, Dict, List

from utils.logger import get_logger
from utils.device import resolve_device
from utils.settings import CLASSIFIER_LABELS, CLASSIFIER_THRESHOLD, CLASSIFIER_MULTI_LABEL
from .base import ModelBase

logger = get_logger(__name__)


class ClassifierModel(ModelBase):
    # Quanti testi per chiamata (limita il picco di attivazioni su GPU).
    INFER_BATCH = 16

    def load(self):
        """Carica la pipeline GliClass una volta. GliClass ≠ GLiNER → import e
        costruzione dedicati (per questo NON riusa ModelBase.load)."""
        if not self.enabled or self._failed:
            return None
        if self._model is not None:
            return self._model
        try:
            from gliclass import GLiClassModel, ZeroShotClassificationPipeline
            from transformers import AutoTokenizer

            device = resolve_device(self.device_pref, what=self.what)
            logger.info(f"Caricamento {self.what} '{self.model_id}' su {device} (one-time)…")
            model = GLiClassModel.from_pretrained(self.model_id)
            tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            ctype = "multi-label" if CLASSIFIER_MULTI_LABEL else "single-label"
            self._model = ZeroShotClassificationPipeline(
                model, tokenizer, classification_type=ctype, device=device,
            )
            self.device = device
            logger.info(f"✅ {self.what} pronto — device={self.device} · model={self.model_id}")
            return self._model
        except Exception as e:
            self._failed = True
            logger.warning(f"{self.what} non disponibile (serve `pip install gliclass`?): {e}")
            return None

    def classify(
        self, texts: List[str], labels: List[str] | None = None, threshold: float | None = None
    ) -> List[List[Dict[str, Any]]]:
        """Classifica ogni testo sulle label. Ritorna una lista allineata a `texts`,
        ognuna con [{label, score}] sopra soglia, ordinate per score desc. Best-effort."""
        labels = labels or CLASSIFIER_LABELS
        thr = CLASSIFIER_THRESHOLD if threshold is None else threshold
        results: List[List[Dict[str, Any]]] = [[] for _ in texts]
        pipe = self.load()
        if pipe is None or not texts or not labels:
            return results
        try:
            indexed = list(enumerate(texts))
            for sub in self._batched(indexed, self.INFER_BATCH):
                idxs = [i for i, _ in sub]
                subtexts = [t for _, t in sub]
                out = pipe(subtexts, labels, threshold=thr)
                # GliClass: lista per-testo di [{'label','score'}]; per un singolo
                # testo alcune versioni ritornano direttamente la lista → normalizza.
                if subtexts and out and isinstance(out[0], dict):
                    out = [out]
                for k, preds in zip(idxs, out or []):
                    rows = [
                        {"label": p["label"], "score": float(p.get("score", 0.0))}
                        for p in (preds or [])
                        if float(p.get("score", 0.0)) >= thr
                    ]
                    rows.sort(key=lambda r: r["score"], reverse=True)
                    results[k] = rows
        except Exception as e:
            logger.warning(f"{self.what} classify fallita: {e}")
        finally:
            self._empty_cache()
        return results
