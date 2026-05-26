"""
Entity extraction deterministica (M3 knowledge graph): GLiNER zero-shot per le
entità "soft" (organizzazione, persona, normativa, data, importo, luogo…) +
regex per quelle strutturate del dominio bancario (IBAN, codice fiscale, P.IVA),
dove un pattern è più preciso e robusto di un modello.

Niente LLM. GLiNER gira su CPU (~50ms/chunk), modello caricato una volta (lazy).
Tutto best-effort: se GLiNER non è disponibile, restano le regex; se entrambi
falliscono si ritorna lista vuota e l'ingestion prosegue.

Output per entità: {name, type, normalized_name, score}. Il `normalized_name`
serve all'entity resolution cross-documento (MERGE sullo stesso nodo).
"""

import re
from typing import Any, Dict, List

from utils.logger import get_logger
from utils.device import resolve_device
from utils.settings import (
    GLINER_ENABLED, GLINER_MODEL, GLINER_THRESHOLD, GLINER_LABELS, GLINER_DEVICE,
)

logger = get_logger(__name__)

# --- GLiNER lazy singleton ---
_model = None
_model_failed = False
_device = "cpu"  # device reale dei pesi, per un empty_cache mirato

# Quante finestre passare a model.inference() per chiamata. Passarle TUTTE insieme
# (batch illimitato) faceva esplodere la VRAM su GPU: mDeBERTa ha attention O(seq²)
# e le attivazioni di ~100+ finestre restavano in cache (≈+2GB). 16 tiene il picco
# basso senza penalizzare il throughput (su CPU è ininfluente).
_GLINER_INFER_BATCH = 16


def _empty_cache() -> None:
    """Libera la cache CUDA dopo un batch (la VRAM torna a riposo invece di restare
    al picco di attivazioni). No-op su CPU."""
    if _device.startswith("cuda"):
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def _get_model():
    global _model, _model_failed, _device
    if not GLINER_ENABLED or _model_failed:
        return None
    if _model is not None:
        return _model
    try:
        from gliner import GLiNER

        device = resolve_device(GLINER_DEVICE, what="GLiNER")
        logger.info(f"Caricamento GLiNER '{GLINER_MODEL}' su {device} (one-time)…")
        _model = GLiNER.from_pretrained(GLINER_MODEL)
        # Device scelto dal dev (default CPU: la GPU resta a embeddings/parser).
        try:
            _model = _model.to(device)
        except Exception as e:
            logger.warning(f"GLiNER .to({device}) fallito, resto su CPU: {e}")
        # Logga il device reale dei pesi: così nei log è inequivocabile.
        try:
            dev = next(_model.model.parameters()).device
        except Exception:
            dev = device
        _device = str(dev)
        logger.info(f"✅ GLiNER pronto — device={dev} · model={GLINER_MODEL}")
        return _model
    except Exception as e:
        _model_failed = True
        logger.warning(f"GLiNER non disponibile, resto sulle sole regex: {e}")
        return None


def warmup() -> None:
    """Pre-carica GLiNER (best-effort) così device e stato finiscono nei log
    all'avvio del worker, invece che al primo chunk processato."""
    if GLINER_ENABLED:
        _get_model()


# --- Regex per entità strutturate (auto-identificanti, basso falso-positivo) ---
# Ognuna: (type, pattern, normalizer). Il normalizer produce normalized_name.
def _norm_compact(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


_REGEX_RULES = [
    ("iban", re.compile(r"\bIT\d{2}[A-Z]\d{22}\b", re.IGNORECASE), _norm_compact),
    ("codice fiscale", re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b"), _norm_compact),
    # P.IVA solo con contesto esplicito, per evitare falsi positivi su numeri da 11 cifre
    ("partita iva", re.compile(r"(?:partita\s+iva|p\.?\s*iva)[\s:.]*?(\d{11})", re.IGNORECASE), _norm_compact),
]


def _normalize(name: str) -> str:
    """Normalizzazione per entity resolution: lowercase, spazi compattati,
    punteggiatura di bordo rimossa."""
    return re.sub(r"\s+", " ", name.strip().lower()).strip(" .,;:·•-")


def _regex_entities(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for etype, pattern, normalizer in _REGEX_RULES:
        for match in pattern.finditer(text):
            # se c'è un gruppo di cattura usa quello (es. la P.IVA senza il prefisso)
            raw = match.group(1) if match.groups() else match.group(0)
            out.append({
                "name": raw.strip(),
                "type": etype,
                "normalized_name": normalizer(raw),
                "score": 1.0,
            })
    return out


def _dedup(ents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tiene una entità per (type, normalized_name), con lo score massimo."""
    best: Dict[tuple, Dict[str, Any]] = {}
    for e in ents:
        if not e["normalized_name"]:
            continue
        key = (e["type"], e["normalized_name"])
        if key not in best or e["score"] > best[key]["score"]:
            best[key] = e
    return list(best.values())


# GLiNER tronca a max_len token (384 per mdeberta-base). I nostri chunk sono più
# lunghi → la NER va fatta a finestre, altrimenti le entità nella coda si perdono.
# Finestre in parole (≈ token) con overlap per non spezzare entità sul confine.
_WINDOW_WORDS = 300
_WINDOW_OVERLAP = 30


def _windows(text: str) -> List[str]:
    words = text.split()
    if len(words) <= _WINDOW_WORDS:
        return [text]
    out: List[str] = []
    step = _WINDOW_WORDS - _WINDOW_OVERLAP
    for start in range(0, len(words), step):
        out.append(" ".join(words[start:start + _WINDOW_WORDS]))
        if start + _WINDOW_WORDS >= len(words):
            break
    return out


def extract_entities_batch(
    texts: List[str], labels: List[str] | None = None
) -> List[List[Dict[str, Any]]]:
    """Estrae le entità per una lista di testi (un chunk per testo).

    `labels`: schema entità per-collection (vedi utils.store_schema); se None usa
    il default globale GLINER_LABELS. GLiNER su finestre (per non troncare i chunk
    lunghi) + regex sul testo intero. Ritorna una lista allineata a `texts`:
    results[i] = entità del chunk i. Best-effort.
    """
    gliner_labels = labels or GLINER_LABELS
    n = len(texts)
    results: List[List[Dict[str, Any]]] = [[] for _ in range(n)]

    # GLiNER (soft entities): espandi i chunk in finestre, tieni traccia del chunk d'origine
    model = _get_model()
    if model is not None and texts:
        segments: List[str] = []
        owners: List[int] = []
        for i, text in enumerate(texts):
            for w in _windows(text):
                segments.append(w)
                owners.append(i)
        try:
            # mini-batch delle finestre: limita il picco di attivazioni su GPU
            batch: List[List[Dict[str, Any]]] = []
            for s in range(0, len(segments), _GLINER_INFER_BATCH):
                batch.extend(
                    model.inference(
                        segments[s:s + _GLINER_INFER_BATCH], gliner_labels,
                        threshold=GLINER_THRESHOLD,
                    )
                )
            for seg_i, ents in enumerate(batch):
                owner = owners[seg_i]
                for e in ents:
                    name = e["text"].strip()
                    # scarta entità troppo lunghe: quasi sempre rumore, non entità
                    if len(name) > 80 or len(name.split()) > 12:
                        continue
                    results[owner].append({
                        "name": name,
                        "type": e["label"],
                        "normalized_name": _normalize(name),
                        "score": float(e.get("score", 0.0)),
                    })
        except Exception as e:
            logger.warning(f"GLiNER inference failed: {e}")
        finally:
            _empty_cache()  # rilascia la cache CUDA del picco di attivazioni

    # Regex (structured entities) sul testo intero (nessun limite di lunghezza)
    for i, text in enumerate(texts):
        results[i].extend(_regex_entities(text))
        results[i] = _dedup(results[i])

    return results
