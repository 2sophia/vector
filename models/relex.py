"""
RelexModel — estrazione di relazioni tipizzate (M5 knowledge graph).

GLiNER-relex zero-shot: oltre a "queste entità compaiono insieme", estrae relazioni
TIPIZZATE tra entità (es. "Decreto —ai sensi di→ D.lgs. 231/2001") in una passata
joint NER+RE. Le entità testa/coda sono normalizzate con la STESSA `normalize` della
NER → l'id `"{type}::{normalized_name}"` coincide con i nodi :Entity (entity
resolution cross-layer). Il modello è **config** (RELATIONS_MODEL): classe agnostica.

Default OFF a livello globale (RELATIONS_ENABLED), ma un singolo store può abilitarlo
(schema a cascata) → la classe carica on-demand, il gating "estraggo o no" sta nel
worker. Mini-batch dei testi per non far esplodere la VRAM su GPU.
"""

import re
from typing import Any, Dict, List, Tuple

from utils.logger import get_logger
from utils.settings import (
    RELATIONS_LABELS, RELATIONS_ENTITY_THRESHOLD, RELATIONS_THRESHOLD,
    RELATIONS_ADJACENCY_THRESHOLD, RELATIONS_MAX_ENTITY_WORDS,
    RELATIONS_DROP_OTHER_TO_OTHER, GLINER_LABELS,
)
from .base import ModelBase
from .text import normalize

logger = get_logger(__name__)


class RelexModel(ModelBase):
    # Testi (chunk interi) per chiamata di inference: relex non lavora a finestre.
    INFER_BATCH = 8

    @staticmethod
    def _too_long(name: str) -> bool:
        """Estremo "lungo" = quasi sempre una frase, non un'entità → rumore.
        Filtro agnostico (non dipende dal dominio)."""
        return len(name) > 60 or len(name.split()) > RELATIONS_MAX_ENTITY_WORDS

    @staticmethod
    def _resolve_ref(ref: Any, ents: List[Dict[str, Any]]) -> Tuple[str, str]:
        """Risolve l'estremo (head/tail) in (testo, label). relex può referenziare
        l'entità per `entity_idx` o passare il dict. Il testo è ripulito dal
        whitespace interno (span a cavallo di un a-capo) → coerente con la NER."""
        if isinstance(ref, dict):
            idx = ref.get("entity_idx")
            if isinstance(idx, int) and 0 <= idx < len(ents):
                e = ents[idx]
                text, label = e.get("text", ""), e.get("label", "")
            else:
                text, label = ref.get("text", ""), ref.get("label") or ref.get("type") or ""
        else:
            text, label = (str(ref) if ref is not None else ""), ""
        return re.sub(r"\s+", " ", text).strip(), label

    def extract(
        self,
        texts: List[str],
        entity_labels: List[str] | None = None,
        relation_labels: List[str] | None = None,
    ) -> List[List[Dict[str, Any]]]:
        """Estrae relazioni tipizzate per una lista di testi (un chunk per testo).
        Ritorna una lista allineata a `texts`: ogni relazione è {head_name, head_type,
        head_norm, tail_name, tail_type, tail_norm, relation, score}, deduplicata per
        (head, tipo, tail) con filtri di igiene agnostici. Best-effort."""
        n = len(texts)
        out: List[List[Dict[str, Any]]] = [[] for _ in range(n)]
        model = self.load()
        if model is None or not texts:
            return out

        ent_labels = list(entity_labels or GLINER_LABELS) + ["other"]
        rel_labels = list(relation_labels or RELATIONS_LABELS)
        if not rel_labels:
            return out

        try:
            ents_batch: List[Any] = []
            rels_batch: List[Any] = []
            for sub in self._batched(texts, self.INFER_BATCH):
                eb, rb = model.inference(
                    texts=sub,
                    labels=ent_labels,
                    relations=rel_labels,
                    threshold=RELATIONS_ENTITY_THRESHOLD,
                    relation_threshold=RELATIONS_THRESHOLD,
                    adjacency_threshold=RELATIONS_ADJACENCY_THRESHOLD,
                    return_relations=True,
                    flat_ner=False,
                )
                ents_batch.extend(eb)
                rels_batch.extend(rb)
        except Exception as e:
            logger.warning(f"GLiNER-relex inference failed: {e}")
            return out
        finally:
            self._empty_cache()

        for i in range(n):
            ents = ents_batch[i] if i < len(ents_batch) else []
            rels = rels_batch[i] if i < len(rels_batch) else []
            best: Dict[tuple, Dict[str, Any]] = {}
            for r in rels:
                htext, hlabel = self._resolve_ref(r.get("head"), ents)
                ttext, tlabel = self._resolve_ref(r.get("tail"), ents)
                rtype = r.get("relation") or r.get("label")
                if not (htext and ttext and rtype):
                    continue
                if self._too_long(htext) or self._too_long(ttext):
                    continue
                hnorm, tnorm = normalize(htext), normalize(ttext)
                htype, ttype = (hlabel or "other"), (tlabel or "other")
                if RELATIONS_DROP_OTHER_TO_OTHER and htype == "other" and ttype == "other":
                    continue
                if not (hnorm and tnorm) or (htype, hnorm) == (ttype, tnorm):
                    continue
                key = (htype, hnorm, rtype, ttype, tnorm)
                score = float(r.get("score", 0.0))
                if key not in best or score > best[key]["score"]:
                    best[key] = {
                        "head_name": htext, "head_type": htype, "head_norm": hnorm,
                        "tail_name": ttext, "tail_type": ttype, "tail_norm": tnorm,
                        "relation": rtype, "score": score,
                    }
            out[i] = list(best.values())
        return out
