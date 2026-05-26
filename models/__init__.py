"""
models/ — home dei modelli in-process (GLiNER-family), con classi dedicate.

- `ModelBase`  : load riusabile + device + empty_cache + batching (generico)
- `NerModel`   : estrazione entità (GLiNER + regex)
- `RelexModel` : estrazione relazioni tipizzate (GLiNER-relex)
- `registry`   : singleton che istanzia e riusa i modelli; `warmup_all()` allo start

Uso (nel worker): `from models import registry` → `registry.warmup_all()` al boot,
poi `registry.ner.extract(...)` / `registry.relex.extract(...)`.
"""

from .base import ModelBase
from .ner import NerModel
from .relex import RelexModel
from .registry import ModelRegistry, registry
from .text import normalize

__all__ = ["ModelBase", "NerModel", "RelexModel", "ModelRegistry", "registry", "normalize"]
