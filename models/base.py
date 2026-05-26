"""
ModelBase — base riusabile per i modelli GLiNER-family.

Incapsula ciò che prima era sparso (singleton lazy + device + batching) in ogni
modulo: caricamento una-tantum riusabile, device configurabile (CPU/GPU), rilascio
mirato della cache CUDA, e l'helper di batching. È **generico**: il modello è una
stringa (config) → cambiarlo è un A/B test, non un refactor. Le sottoclassi
(NerModel, RelexModel) aggiungono solo il `extract()` specifico.

Best-effort: se il modello non carica, `load()` ritorna None e il chiamante
degrada con grazia (la NER cade sulle regex, il relex salta le relazioni).
"""

from typing import Any, Iterator, List, Sequence

from utils.logger import get_logger
from utils.device import resolve_device

logger = get_logger(__name__)


class ModelBase:
    def __init__(self, model_id: str, device_pref: str, what: str, enabled: bool = True):
        self.model_id = model_id
        self.device_pref = device_pref
        self.what = what
        self.enabled = enabled
        self._model = None
        self._failed = False
        self.device = "cpu"  # device reale dei pesi, per un empty_cache mirato

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self):
        """Carica il modello una volta e lo riusa. Idempotente, thread-safe-enough
        per il nostro worker single-thread per modello."""
        if not self.enabled or self._failed:
            return None
        if self._model is not None:
            return self._model
        try:
            from gliner import GLiNER

            device = resolve_device(self.device_pref, what=self.what)
            logger.info(f"Caricamento {self.what} '{self.model_id}' su {device} (one-time)…")
            model = GLiNER.from_pretrained(self.model_id)
            try:
                model = model.to(device)
            except Exception as e:
                logger.warning(f"{self.what} .to({device}) fallito, resto su CPU: {e}")
                device = "cpu"
            self._model = model
            try:
                self.device = str(next(model.model.parameters()).device)
            except Exception:
                self.device = device
            logger.info(f"✅ {self.what} pronto — device={self.device} · model={self.model_id}")
            return self._model
        except Exception as e:
            self._failed = True
            logger.warning(f"{self.what} non disponibile: {e}")
            return None

    def _empty_cache(self) -> None:
        """Libera la cache CUDA dopo un batch (la VRAM torna a riposo invece di
        restare al picco di attivazioni). No-op su CPU."""
        if self.device.startswith("cuda"):
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    @staticmethod
    def _batched(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
        """Spezza in mini-batch: limita il picco di attivazioni su GPU (mDeBERTa ha
        attention O(seq²), passare tutto insieme esplode la VRAM)."""
        for start in range(0, len(items), size):
            yield list(items[start:start + size])
