#!/usr/bin/env python3
"""Audit eval — misura la qualità della search sul gold set (un campione di documenti).

Per ogni configurazione (soglia rerank, graph_expand) calcola:
  - POSITIVI: recall@1/5/10 + MRR + score del primo hit corretto (precisione/recall)
  - NEGATIVI: quante query "fuori corpus" tornano risultati e con che score (chirurgicità:
    deve tornare vuoto se la risposta non c'è)

Usa la funzione reale search_vector_store (stessa che serve l'endpoint /v1/*).

    .venv/bin/python scripts/audit_eval.py
"""
import os, sys, json
os.environ.setdefault("PYTHONWARNINGS", "ignore")
import logging; logging.disable(logging.INFO)

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))

from routers.search import search_vector_store
from utils.schemas import VectorSearch, RankingOptions

# gold set: usa il file reale/privato (gitignorato) se c'è, altrimenti il template generico
_gold_local = os.path.join(HERE, "audit_gold.local.json")
gold = json.load(open(_gold_local if os.path.exists(_gold_local) else os.path.join(HERE, "audit_gold.json")))
manifest = json.load(open(os.path.join(HERE, "audit_manifest.json")))
VS = manifest["vector_store_id"]

POS = gold["positives"]
NEG = gold["negatives"]


def run(q, threshold, graph=False, k=10):
    sd = VectorSearch(
        query=q, max_num_results=k, graph_expand=graph,
        ranking_options=RankingOptions(score_threshold=threshold),
    )
    res = search_vector_store(VS, sd)
    return res.get("data", [])


import re
def _norm(s):
    # i filename vengono slugificati all'ingest (lowercase, non-alfanumerici → '-').
    # Normalizziamo expect e filename allo stesso modo per un match robusto.
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")

def rank_of(rows, expect):
    exp = _norm(expect)
    for i, r in enumerate(rows):
        if exp in _norm(r.get("filename")):
            return i, r.get("score")
    return None, None


def eval_config(name, threshold, graph=False):
    # positivi
    r1 = r5 = r10 = 0
    rr = 0.0
    hit_scores, miss = [], []
    for item in POS:
        rows = run(item["q"], threshold, graph)
        idx, score = rank_of(rows, item["expect"])
        if idx is not None:
            if idx == 0: r1 += 1
            if idx < 5: r5 += 1
            if idx < 10: r10 += 1
            rr += 1.0 / (idx + 1)
            hit_scores.append(score or 0.0)
        else:
            miss.append(item["expect"])
    n = len(POS)
    # negativi
    neg_hits, neg_maxscores = 0, []
    for q in NEG:
        rows = run(q, threshold, graph)
        if rows:
            neg_hits += 1
            neg_maxscores.append(max((r.get("score") or 0.0) for r in rows))
    print(f"\n### {name}")
    print(f"  POSITIVI ({n}): recall@1={r1/n:.0%}  recall@5={r5/n:.0%}  recall@10={r10/n:.0%}  MRR={rr/n:.3f}")
    if hit_scores:
        print(f"           score hit: min={min(hit_scores):.3f}  mean={sum(hit_scores)/len(hit_scores):.3f}  max={max(hit_scores):.3f}")
    if miss:
        print(f"           MISS ({len(miss)}): " + "; ".join(m[:35] for m in miss))
    nn = len(NEG)
    print(f"  NEGATIVI ({nn}): query con risultati = {neg_hits}/{nn}" + (
        f"  (max-score: min={min(neg_maxscores):.3f} mean={sum(neg_maxscores)/len(neg_maxscores):.3f} max={max(neg_maxscores):.3f})" if neg_maxscores else "  → tutte vuote ✓"))
    return {"name": name, "recall@5": r5/n, "recall@10": r10/n, "mrr": rr/n,
            "neg_leak": neg_hits, "hit_min": min(hit_scores) if hit_scores else None,
            "neg_max": max(neg_maxscores) if neg_maxscores else 0.0}


if __name__ == "__main__":
    print(f"Gold: {len(POS)} positivi + {len(NEG)} negativi | store {VS}")
    summary = []
    summary.append(eval_config("soglia 0.0 (nessun taglio)", 0.0))
    summary.append(eval_config("soglia 0.1 (DEFAULT attuale)", 0.1))
    summary.append(eval_config("soglia 0.3", 0.3))
    summary.append(eval_config("soglia 0.5", 0.5))
    summary.append(eval_config("soglia 0.1 + graph_expand", 0.1, graph=True))

    print("\n\n===== RIEPILOGO =====")
    print(f"{'config':<32} {'rec@5':>6} {'rec@10':>7} {'MRR':>6} {'neg_leak':>9} {'hit_min':>8} {'neg_max':>8}")
    for s in summary:
        print(f"{s['name']:<32} {s['recall@5']:>6.0%} {s['recall@10']:>7.0%} {s['mrr']:>6.3f} "
              f"{s['neg_leak']:>9} {(s['hit_min'] or 0):>8.3f} {s['neg_max']:>8.3f}")
