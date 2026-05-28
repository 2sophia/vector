"""Stadio di ranking finale — applicato DOPO il cross-encoder.

Perché qui e non come query nativa Qdrant: il rerank di sophia-vector è esterno
(cross-encoder BGE). Le feature native di Qdrant (MMR, grouping, score-boosting)
agirebbero *prima* del rerank, che poi ri-ordina e ne annulla l'effetto. Per avere
effetto sui top-k consegnati all'LLM vanno applicate **dopo** il rerank — è ciò che
fa questo modulo, sul pool reranked (fino a max_rerank_results) prima del taglio
finale a max_num_results.

Tutto è **opt-in con default off**: a parametri di default `apply_final_ranking`
ritorna il pool invariato (solo tagliato), quindi il comportamento storico non cambia.

Leve (in RankingOptions):
  - recency_half_life_days  → boost di freschezza (decay esponenziale sulla data doc)
  - mmr_diversity           → diversificazione MMR (anti near-duplicate semantici)
  - group_by_file_max       → max N chunk per documento nei risultati (diversità fonti)
"""

from datetime import datetime, timezone
from typing import List, Tuple, Any, Optional

import numpy as np

from .logger import get_logger

logger = get_logger(__name__)

# campi data del payload, in ordine di preferenza (il primo non-nullo vince)
_DATE_FIELDS = ("sharepoint_file_modified", "sharepoint_file_created", "created_at")


def _parse_epoch(payload: dict) -> Optional[float]:
    """Estrae un timestamp (epoch secondi) dal payload. Gestisce ISO-8601 ('...Z')
    e epoch numerici. None se non c'è una data utilizzabile."""
    for f in _DATE_FIELDS:
        v = payload.get(f)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def _apply_recency(scored: List[Tuple[Any, float]], half_life_days: float, weight: float):
    """Blend tra rilevanza e freschezza: score' = score · ((1-w) + w·decay), con
    decay = 0.5^(età/half_life) ∈ (0,1]. weight=0 → no-op; documenti senza data →
    decay neutro (1.0), non penalizzati. Non stravolge l'ordine (blend, non sostituzione)."""
    now = datetime.now(timezone.utc).timestamp()
    hl = max(half_life_days, 0.1) * 86400.0
    w = min(max(weight, 0.0), 1.0)
    out = []
    for point, score in scored:
        epoch = _parse_epoch(point.payload or {})
        if epoch is None:
            out.append((point, score))
            continue
        age = max(now - epoch, 0.0)
        decay = 0.5 ** (age / hl)
        out.append((point, score * ((1.0 - w) + w * decay)))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _mmr_order(scored, vectors_by_id, diversity: float, top_k: int):
    """Maximal Marginal Relevance greedy: bilancia rilevanza e novità.
        MMR = λ·rel − (1−λ)·max_sim(candidato, già_scelti),  λ = 1 − diversity
    diversity 0 → pura rilevanza (no-op), alto → più diversità. Usa i vettori dense
    (cosine). I punti senza vettore restano ordinati per rilevanza in coda."""
    lam = 1.0 - min(max(diversity, 0.0), 1.0)

    # normalizza i vettori disponibili (cosine = dot di vettori unitari)
    ids = [str(p.id) for p, _ in scored if str(p.id) in vectors_by_id]
    if len(ids) < 2:
        return scored  # niente da diversificare
    mat = np.array([vectors_by_id[i] for i in ids], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    idx_of = {i: k for k, i in enumerate(ids)}

    rel = {str(p.id): s for p, s in scored}
    # normalizza la rilevanza in [0,1] per renderla comparabile alla cosine
    rvals = np.array(list(rel.values()), dtype=np.float32)
    rmin, rmax = float(rvals.min()), float(rvals.max())
    span = (rmax - rmin) or 1.0
    reln = {k: (v - rmin) / span for k, v in rel.items()}

    candidates = [str(p.id) for p, _ in scored if str(p.id) in idx_of]
    selected: List[str] = []
    limit = min(top_k, len(candidates))
    while candidates and len(selected) < limit:
        best_id, best_mmr = None, -1e9
        for cid in candidates:
            if selected:
                sims = mat[idx_of[cid]] @ mat[[idx_of[s] for s in selected]].T
                redundancy = float(np.max(sims))
            else:
                redundancy = 0.0
            mmr = lam * reln[cid] - (1.0 - lam) * redundancy
            if mmr > best_mmr:
                best_mmr, best_id = mmr, cid
        selected.append(best_id)
        candidates.remove(best_id)

    point_by_id = {str(p.id): (p, s) for p, s in scored}
    ordered = [point_by_id[i] for i in selected]
    # eventuali punti senza vettore, in coda nell'ordine originale
    ordered += [(p, s) for p, s in scored if str(p.id) not in idx_of]
    return ordered


def _group_by_file(scored, max_per_file: int):
    """Tiene al più `max_per_file` chunk per file_id, preservando l'ordine. Gli
    eccedenti non vengono persi: finiscono in coda (riempiono se restano slot)."""
    kept, overflow, seen = [], [], {}
    for point, score in scored:
        fid = (point.payload or {}).get("file_id")
        n = seen.get(fid, 0)
        if fid is None or n < max_per_file:
            kept.append((point, score))
            seen[fid] = n + 1
        else:
            overflow.append((point, score))
    return kept + overflow


def apply_final_ranking(seed_points, search_data, collection_name, qdrant_client):
    """Riordina il pool reranked (opt-in) e taglia a max_num_results.

    seed_points: List[(ScoredPoint, rerank_score)] ordinata per rilevanza desc.
    A default (nessuna leva attiva) = `seed_points[:max_num_results]`, invariato.
    """
    ro = search_data.ranking_options
    top_k = search_data.max_num_results or 15

    half_life = getattr(ro, "recency_half_life_days", None)
    diversity = getattr(ro, "mmr_diversity", None)
    group_max = getattr(ro, "group_by_file_max", None)

    # fast-path: nessuna leva → comportamento storico
    if not half_life and not diversity and not group_max:
        return seed_points[:top_k]

    scored = list(seed_points)

    if half_life:
        scored = _apply_recency(scored, half_life, getattr(ro, "recency_weight", 0.3) or 0.3)

    if diversity:
        # recupera i vettori dense del pool una volta sola (MMR ne ha bisogno)
        ids = [p.id for p, _ in scored]
        vectors_by_id = {}
        try:
            recs = qdrant_client.retrieve(
                collection_name=collection_name, ids=ids,
                with_payload=False, with_vectors=["dense"],
            )
            for r in recs:
                vec = r.vector.get("dense") if isinstance(r.vector, dict) else r.vector
                if vec is not None:
                    vectors_by_id[str(r.id)] = vec
        except Exception as e:
            logger.warning(f"[mmr] retrieve vettori fallito, salto MMR: {e}")
        if vectors_by_id:
            scored = _mmr_order(scored, vectors_by_id, diversity, top_k=max(top_k * 3, top_k))

    if group_max:
        scored = _group_by_file(scored, int(group_max))

    return scored[:top_k]
