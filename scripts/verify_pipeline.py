#!/usr/bin/env python3
"""
Verifica end-to-end della pipeline di un vector store: ingestion, grafo, data
curation e (opzionale) ricerca. Risponde alla domanda "ma sta andando tutto?".

Uso:
    .venv/bin/python scripts/verify_pipeline.py [vector_store_id] [--query "testo"]

Se ometti il vector_store_id e c'è una sola collection `vs_*`, la prende da sola.
Tutto read-only: ispeziona Mongo + Qdrant + FalkorDB e, con --query, fa una
ricerca vera via backend HTTP mostrando se la soppressione boilerplate scatta.
"""

import os
import sys
import argparse
import collections

# rendi importabile il pacchetto utils anche lanciando lo script direttamente
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.database import db
from utils.qdrant import qdrant_client
from utils.falkor import _graph
from utils.curation import curation_stats
from utils.settings import (
    CURATION_BOILERPLATE_RATIO, CURATION_BOILERPLATE_MIN_DOCS, INTERNAL_API_URL,
)


def _hr(title):
    print(f"\n{'─' * 4} {title} {'─' * (60 - len(title))}")


def resolve_vs(arg):
    cols = [c.name for c in qdrant_client.get_collections().collections]
    vs_cols = [c for c in cols if c.startswith("vs_")]
    if arg:
        if arg not in cols:
            sys.exit(f"❌ collection {arg} non esiste. Disponibili: {vs_cols}")
        return arg
    if len(vs_cols) == 1:
        print(f"(auto) unico vector store: {vs_cols[0]}")
        return vs_cols[0]
    sys.exit(f"Specifica un vector_store_id. Disponibili: {vs_cols}")


def check_jobs(vs):
    _hr("Ingestion (Mongo)")
    jobs = db["ingestion_jobs"]
    by_status = collections.Counter(
        j["status"] for j in jobs.find({"vector_store_id": vs}, {"status": 1})
    )
    print("job per stato:", dict(by_status))
    # file validi ma senza testo: il flag stats.empty (nuovo) oppure num_chunks=0
    # (job ingeriti prima del flag) — entrambi indicano "0 chunk estratti".
    empty = list(jobs.find(
        {"vector_store_id": vs,
         "$or": [{"stats.empty": True}, {"stats.num_chunks": 0}],
         "status": "COMPLETED"},
        {"filename": 1},
    ))
    failed = list(jobs.find(
        {"vector_store_id": vs, "status": "FAILED"}, {"filename": 1, "error": 1}
    ))
    if empty:
        print(f"⚠️  {len(empty)} file con 0 chunk (validi ma senza testo estraibile):")
        for j in empty[:10]:
            print("    -", j.get("filename"))
    if failed:
        print(f"⚠️  {len(failed)} file FAILED:")
        for j in failed[:10]:
            print("    -", j.get("filename"), "|", (j.get("error") or "")[:70])
    if not empty and not failed:
        print("✅ nessun file rotto/vuoto/FAILED")


def check_qdrant(vs):
    _hr("Qdrant")
    try:
        info = qdrant_client.get_collection(vs)
        print("punti indicizzati:", info.points_count)
    except Exception as e:
        print("errore lettura collection:", e)


def check_graph(vs):
    _hr("Knowledge graph (FalkorDB)")
    g = _graph(vs)
    if g is None:
        print("grafo non disponibile (GRAPH_ENABLED off o FalkorDB giù)")
        return
    for lbl in ("Document", "Section", "Chunk", "Entity", "Content"):
        try:
            n = g.query(f"MATCH (n:{lbl}) RETURN count(n)").result_set[0][0]
            print(f"  :{lbl:<9} {n}")
        except Exception as e:
            print(f"  :{lbl} errore {e}")
    try:
        bh = g.query(
            "MATCH (c:Chunk) WHERE c.body_hash IS NOT NULL RETURN count(c)"
        ).result_set[0][0]
        print(f"  chunk con body_hash: {bh}")
    except Exception:
        pass


def check_curation(vs):
    _hr("Data curation (dedup contenuto)")
    stats = curation_stats(vs, CURATION_BOILERPLATE_RATIO, CURATION_BOILERPLATE_MIN_DOCS)
    print("stats:", stats)
    print(f"soglie attive: ratio={CURATION_BOILERPLATE_RATIO} min_docs={CURATION_BOILERPLATE_MIN_DOCS}")

    rows = list(db["curation_bodies"].find(
        {"vector_store_id": vs}, {"files": 1, "body_hash": 1}
    ))
    total = stats.get("total_documents") or 1
    freq = collections.Counter(len(r.get("files") or []) for r in rows)
    repeated = {k: c for k, c in freq.items() if k >= 2}
    print("contenuti unici (1 doc):", freq.get(1, 0))
    if repeated:
        print("distribuzione contenuti ripetuti (in N doc → quanti):")
        for k in sorted(repeated, reverse=True):
            print(f"    in {k} doc ({k/total:.0%}): {repeated[k]}")
        print("verrebbero soppressi a varie soglie (min_docs=2):")
        for ratio in (0.5, 0.33, 0.25, 0.2):
            n = sum(c for k, c in freq.items() if k >= 2 and k / total >= ratio)
            print(f"    ratio={ratio} → {n}")
        # anteprima testo dei più ripetuti
        g = _graph(vs)
        top = sorted(rows, key=lambda r: len(r.get("files") or []), reverse=True)[:3]
        print("top contenuti ripetuti (anteprima):")
        for r in top:
            nd = len(r.get("files") or [])
            txt = ""
            if g is not None:
                try:
                    rs = g.query(
                        "MATCH (c:Chunk {body_hash:$h}) RETURN c.text LIMIT 1",
                        {"h": r.get("body_hash")},
                    ).result_set
                    if rs:
                        txt = (rs[0][0] or "")[:140].replace("\n", " ")
                except Exception:
                    pass
            print(f"    [{nd} doc] {txt!r}")
    else:
        print("nessun contenuto ripetuto: corpus a bassa ridondanza (niente da sopprimere)")


def check_search(vs, query):
    _hr(f"Ricerca live: {query!r}")
    import requests
    url = f"{INTERNAL_API_URL}/v1/vector_stores/{vs}/search"
    try:
        r = requests.post(url, json={"query": query, "max_num_results": 10}, timeout=60)
        r.raise_for_status()
        data = r.json().get("data", [])
        print(f"risultati: {len(data)}")
        for d in data[:5]:
            print(f"  · score={d.get('score')!s:.6} {(d.get('content') or '')[:90]!r}")
    except Exception as e:
        print("ricerca non eseguita (backend giù?):", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vector_store_id", nargs="?")
    ap.add_argument("--query", help="esegue anche una ricerca live via backend HTTP")
    args = ap.parse_args()

    vs = resolve_vs(args.vector_store_id)
    print(f"=== verifica pipeline · {vs} ===")
    check_jobs(vs)
    check_qdrant(vs)
    check_graph(vs)
    check_curation(vs)
    if args.query:
        check_search(vs, args.query)
    print()


if __name__ == "__main__":
    main()
