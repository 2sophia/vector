"""
Relation extraction (M5 knowledge graph): GLiNER-relex zero-shot.

Layer ADDITIVO sopra l'estrazione entità (utils/entities). Mentre la NER + il
grafo strutturale danno "queste entità compaiono nello stesso chunk", qui
estraiamo relazioni TIPIZZATE tra entità (es. "Decreto —ai sensi di→ D.lgs.
231/2001", "Documento —pubblicato da→ Organizzazione") con un modello GLiNER-relex
che fa NER+RE in una passata joint, zero-shot (le label di relazione si passano a
runtime). Stesso pattern di utils/entities: lazy singleton, device configurabile,
best-effort (un guasto qui non rompe l'ingestion).

Le entità testa/coda vengono normalizzate con la STESSA `_normalize` di
utils/entities, così l'id `"{type}::{normalized_name}"` coincide con quello dei
nodi :Entity creati dalla NER → gli archi :REL agganciano gli stessi nodi (entity
resolution cross-layer), non ne creano di paralleli.

Default OFF (`RELATIONS_ENABLED`): è un secondo modello in RAM nel worker. Si
abilita quando si vuole il grafo con relazioni tipizzate.
"""

from typing import Any, Dict, List, Tuple

from utils.logger import get_logger
from utils.device import resolve_device
from utils.entities import _normalize  # stessa normalizzazione → stessi id :Entity
from utils.settings import (
    RELATIONS_ENABLED, RELATIONS_MODEL, RELATIONS_LABELS,
    RELATIONS_ENTITY_THRESHOLD, RELATIONS_THRESHOLD, RELATIONS_ADJACENCY_THRESHOLD,
    RELATIONS_MAX_ENTITY_WORDS, RELATIONS_DROP_OTHER_TO_OTHER,
    GLINER_LABELS, GLINER_DEVICE,
)

logger = get_logger(__name__)

_model = None
_model_failed = False
_device = "cpu"  # device reale dei pesi, per un empty_cache mirato

# Testi per chiamata di inference. relex lavora sui CHUNK INTERI (non a finestre) e
# fa NER+RE joint: passarli tutti insieme fa esplodere la VRAM su GPU. 8 tiene il
# picco basso (su CPU è ininfluente).
_RELEX_INFER_BATCH = 8


def _empty_cache() -> None:
    """Libera la cache CUDA dopo un batch. No-op su CPU."""
    if _device.startswith("cuda"):
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def _too_long(name: str) -> bool:
    """Estremo "lungo" = quasi sempre una frase, non un'entità → rumore.
    Filtro agnostico (non dipende dal dominio)."""
    return len(name) > 60 or len(name.split()) > RELATIONS_MAX_ENTITY_WORDS


def _get_model():
    # NB: niente gate sul flag globale RELATIONS_ENABLED qui — un singolo store può
    # attivare le relazioni anche se il default globale è OFF (schema a cascata). Il
    # gating "estraggo o no" sta nel worker (relations_on effettivo); qui carico
    # solo on-demand. _model_failed evita di ritentare il load a ogni chunk.
    global _model, _model_failed, _device
    if _model_failed:
        return None
    if _model is not None:
        return _model
    try:
        from gliner import GLiNER

        device = resolve_device(GLINER_DEVICE, what="GLiNER-relex")
        logger.info(f"Caricamento GLiNER-relex '{RELATIONS_MODEL}' su {device} (one-time)…")
        _model = GLiNER.from_pretrained(RELATIONS_MODEL)
        try:
            _model = _model.to(device)
        except Exception as e:
            logger.warning(f"relex .to({device}) fallito, resto su CPU: {e}")
        _device = device
        logger.info(f"✅ GLiNER-relex pronto — device={device} · model={RELATIONS_MODEL}")
        return _model
    except Exception as e:
        _model_failed = True
        logger.warning(f"GLiNER-relex non disponibile, niente relazioni: {e}")
        return None


def warmup() -> None:
    """Pre-carica il modello relex se abilitato (best-effort)."""
    if RELATIONS_ENABLED:
        _get_model()


def _resolve_ref(ref: Any, ents: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Risolve l'estremo (head/tail) di una relazione in (testo, label).
    GLiNER-relex può referenziare l'entità per `entity_idx` o passare il dict."""
    if isinstance(ref, dict):
        idx = ref.get("entity_idx")
        if isinstance(idx, int) and 0 <= idx < len(ents):
            e = ents[idx]
            return e.get("text", ""), e.get("label", "")
        return ref.get("text", ""), ref.get("label") or ref.get("type") or ""
    return (str(ref) if ref is not None else ""), ""


def extract_relations_batch(
    texts: List[str],
    entity_labels: List[str] | None = None,
    relation_labels: List[str] | None = None,
) -> List[List[Dict[str, Any]]]:
    """Estrae relazioni tipizzate per una lista di testi (un chunk per testo).

    `entity_labels`/`relation_labels`: schema per-collection (vedi utils.store_schema);
    se None usa i default globali. Le label entità ricevono "other" in coda (cattura
    entità inferibili dalle relazioni, feature del modello).

    Ritorna una lista allineata a `texts`: results[i] = relazioni del chunk i, ogni
    relazione è un dict {head_name, head_type, head_norm, tail_name, tail_type,
    tail_norm, relation, score}. Deduplicata per (head, tipo, tail) tenendo lo score
    massimo, con filtri di igiene agnostici (estremi lunghi, other→other). Best-effort.
    """
    n = len(texts)
    out: List[List[Dict[str, Any]]] = [[] for _ in range(n)]
    model = _get_model()
    if model is None or not texts:
        return out

    ent_labels = list(entity_labels or GLINER_LABELS) + ["other"]
    rel_labels = list(relation_labels or RELATIONS_LABELS)
    if not rel_labels:
        return out

    try:
        # mini-batch dei testi: limita il picco di attivazioni su GPU
        ents_batch: List[Any] = []
        rels_batch: List[Any] = []
        for s in range(0, len(texts), _RELEX_INFER_BATCH):
            eb, rb = model.inference(
                texts=texts[s:s + _RELEX_INFER_BATCH],
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
        _empty_cache()  # rilascia la cache CUDA del picco di attivazioni

    for i in range(n):
        ents = ents_batch[i] if i < len(ents_batch) else []
        rels = rels_batch[i] if i < len(rels_batch) else []
        best: Dict[tuple, Dict[str, Any]] = {}
        for r in rels:
            htext, hlabel = _resolve_ref(r.get("head"), ents)
            ttext, tlabel = _resolve_ref(r.get("tail"), ents)
            rtype = r.get("relation") or r.get("label")
            if not (htext and ttext and rtype):
                continue
            # filtro igiene: estremi-frase (rumore) e relazioni tra due "other"
            if _too_long(htext) or _too_long(ttext):
                continue
            hnorm, tnorm = _normalize(htext), _normalize(ttext)
            htype, ttype = (hlabel or "other"), (tlabel or "other")
            if RELATIONS_DROP_OTHER_TO_OTHER and htype == "other" and ttype == "other":
                continue
            # niente self-loop sulla stessa entità normalizzata
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
