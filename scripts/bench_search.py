#!/usr/bin/env python3
"""
Mini-benchmark del retrieval (graph OFF vs ON) su un golden set.

Golden set tarato su un documento bancario ("Testo Unico in materia di Trasparenza").
Per ogni query lancia /v1/vector_stores/{id}/search due volte (graph_expand false/true),
trova a che rank compare il chunk "gold" (contiene TUTTE le keyword attese), e confronta.

Uso:
    .venv/bin/python scripts/bench_search.py [vector_store_id] [--backend http://localhost:8100]

Niente eval harness pesante: è un confronto A/B leggero per capire se il grafo
aiuta, inquina, o è neutro. Recall@k / MRR si leggono dai rank stampati.
"""
import sys
import argparse
import re
import requests

# (query, [keyword tutte presenti nel chunk gold], nota)
GOLDEN = [
    ("Entro quanti giorni la banca deve rispondere a un reclamo?",
     ["15 giorni", "60 giorni"],
     "granularità: il chunk gold ha ENTRAMBI i termini (§5.3)"),
    ("Cos'è l'ISC e a cosa serve?",
     ["indicatore sintetico di costo"],
     "definizione acronimo (§5.2a)"),
    ("Chi verifica i tassi soglia d'usura?",
     ["pianificazione e controlli"],
     "TRAPPOLA: ruolo (tabella pag.9), non la definizione di usura (§5.2e)"),
    ("Quali documenti interni regolano la remunerazione della rete di vendita?",
     ["compensi provvigionali"],
     "lista in fondo a sezione lunga (§5.2c) → test chunk 512 + :NEXT"),
    ("Cosa prevede il jus variandi?",
     ["118"],
     "riferimento normativo art. 118 TUB (pag.14)"),
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def gold_rank(results, keywords):
    """Primo rank (1-based) il cui content contiene TUTTE le keyword. 0 = assente."""
    kws = [norm(k) for k in keywords]
    for i, r in enumerate(results, 1):
        text = norm(r.get("content") or "")
        if all(k in text for k in kws):
            return i
    return 0


def search(backend, vsid, query, graph):
    body = {"query": query, "max_num_results": 10, "graph_expand": graph}
    r = requests.post(f"{backend}/v1/vector_stores/{vsid}/search", json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("data", [])


def fmt(rank):
    return f"rank {rank}" if rank else "ASSENTE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vsid", nargs="?", default="vs_1aa2dcc4fa18")
    ap.add_argument("--backend", default="http://localhost:8100")
    args = ap.parse_args()

    print(f"Benchmark su {args.vsid} ({args.backend})\n" + "=" * 64)
    mrr_off = mrr_on = 0.0
    hit_off = hit_on = 0
    for q, gold, note in GOLDEN:
        off = search(args.backend, args.vsid, q, False)
        on = search(args.backend, args.vsid, q, True)
        r_off = gold_rank(off, gold)
        r_on = gold_rank(on, gold)
        mrr_off += 1.0 / r_off if r_off else 0
        mrr_on += 1.0 / r_on if r_on else 0
        hit_off += 1 if r_off and r_off <= 3 else 0
        hit_on += 1 if r_on and r_on <= 3 else 0
        delta = ""
        if r_off and r_on:
            if r_on < r_off:
                delta = "  ⬆ grafo MIGLIORA"
            elif r_on > r_off:
                delta = "  ⬇ grafo peggiora"
            else:
                delta = "  = invariato"
        elif r_on and not r_off:
            delta = "  ⬆ grafo RECUPERA (era assente)"
        elif r_off and not r_on:
            delta = "  ⬇ grafo PERDE il gold"
        print(f"\n▸ {q}")
        print(f"  gold: {gold}  — {note}")
        print(f"  OFF: {fmt(r_off):10s} | ON: {fmt(r_on):10s}{delta}")
        # top-3 del run ON con provenienza
        for i, r in enumerate(on[:3], 1):
            attr = r.get("attributes", {}) or {}
            src = attr.get("_source", "qdrant")
            via = attr.get("_via")
            via_s = f" via={via}" if via else ""
            print(f"     #{i} [{src}] score={r.get('score')}{via_s}")

    n = len(GOLDEN)
    print("\n" + "=" * 64)
    print(f"Recall@3   OFF: {hit_off}/{n}   ON: {hit_on}/{n}")
    print(f"MRR        OFF: {mrr_off/n:.3f}   ON: {mrr_on/n:.3f}")


if __name__ == "__main__":
    main()
