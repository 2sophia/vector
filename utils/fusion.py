"""
Reciprocal Rank Fusion (RRF) — fonde più liste ordinate in un'unica classifica
senza bisogno di score comparabili tra i canali.

    score(id) = Σ_canali  w / (k + rank)

dove `rank` è la posizione (0-based) dell'id in quel canale. Robusto perché usa
solo il *rango*, non lo score: canali eterogenei (vettoriale, grafo, lessicale)
si fondono senza calibrazione. k=60 è il valore canonico del paper (Cormack 2009).

NB sull'uso in sophia-vector: dense+sparse sono già fusi da Qdrant a monte
(`FusionQuery(RRF)`); il cross-encoder finale è una fusione più forte quando è
acceso. RRF qui serve a: (a) fondere il canale grafo con quello vettoriale come
candidati di prima classe, (b) dare un ordine sensato quando il rerank è off/fallito,
(c) essere pronto quando si aggiunge un canale lessicale/exact-match (lì RRF rende
davvero, perché il dense è debole su codici/riferimenti).
"""

from typing import Dict, List, Optional, Sequence, Tuple

RRF_K = 60


def rrf_fuse(
    ranked_lists: Sequence[Sequence[str]],
    k: int = RRF_K,
    weights: Optional[Sequence[float]] = None,
) -> List[Tuple[str, float]]:
    """Fonde N liste ordinate di id (ognuna best-first) in `[(id, score)]` desc.

    `weights` opzionale, uno per canale (default 1.0 per tutti). Un id che compare
    in più canali somma i contributi → premiato l'accordo tra canali.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    scores: Dict[str, float] = {}
    for lst, w in zip(ranked_lists, weights):
        for rank, _id in enumerate(lst):
            if _id is None:
                continue
            scores[_id] = scores.get(_id, 0.0) + w / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
