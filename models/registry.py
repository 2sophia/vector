"""
ModelRegistry — singleton che possiede le istanze dei modelli e le riusa.

Una sola home per i modelli: il worker chiama `registry.warmup_all()` allo start
(così device e stato finiscono nei log all'avvio, non al primo chunk) e poi usa
`registry.ner.extract(...)` / `registry.relex.extract(...)`. Niente più singleton
sparsi nei moduli. L'API server NON importa questo: i modelli servono solo
all'ingestion (worker), non al retrieval (che usa BGE-M3 via HTTP).

I modelli sono **config-driven**: cambiare GLINER_MODEL / RELATIONS_MODEL è un A/B
test (le classi sono agnostiche al modello).
"""

from utils.logger import get_logger
from utils.settings import (
    GLINER_ENABLED, GLINER_MODEL, GLINER_DEVICE,
    RELATIONS_ENABLED, RELATIONS_MODEL,
)
from .ner import NerModel
from .relex import RelexModel

logger = get_logger(__name__)


class ModelRegistry:
    def __init__(self):
        # NER: gate sul flag globale (se off → solo regex, modello non caricato).
        self.ner = NerModel(GLINER_MODEL, GLINER_DEVICE, "GLiNER", enabled=GLINER_ENABLED)
        # relex: device condiviso con GLiNER; abilitabile per-scope, quindi a livello
        # modello resta "caricabile on-demand" (il gate vero è nel worker).
        self.relex = RelexModel(RELATIONS_MODEL, GLINER_DEVICE, "GLiNER-relex", enabled=True)

    def warmup_all(self) -> None:
        """Pre-carica i modelli necessari allo start del worker (best-effort)."""
        if GLINER_ENABLED:
            self.ner.load()
        # relex pre-caricato solo se globalmente attivo (sennò resta lazy: lo carica
        # il primo store che lo abilita via schema, per non sprecare RAM/VRAM).
        if RELATIONS_ENABLED:
            self.relex.load()


# istanza unica, importata dal worker
registry = ModelRegistry()
