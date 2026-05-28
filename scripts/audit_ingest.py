#!/usr/bin/env python3
"""Audit ingest — carica un campione reale del corpus ViViBanca via le API REST del
backend (NON una pipeline custom: stesso percorso del prodotto, così l'eval è reale).

Seleziona N PDF piccoli per categoria, li uploada (/v1/files) e li attacca a un
vector store (/v1/vector_stores/{id}/files) con uno slug per categoria. Salva un
manifest (file_id → filename, categoria, slug) per costruire il gold set.

    .venv/bin/python scripts/audit_ingest.py
"""
import os, sys, json, time, glob
import requests

BASE = "http://127.0.0.1:8100"
CORPUS = "/home/mwspace/Documenti/OneDrive_1_27-04-2026/A. Documenti ViViBanca"
MANIFEST = os.path.join(os.path.dirname(__file__), "audit_manifest.json")

# categoria cartella → (slug, quanti file, dim max byte)
PLAN = [
    ("Antiriciclaggio",  "antiriciclaggio", 8, 2_000_000),
    ("GDPR Privacy",     "gdpr-privacy",    8, 2_000_000),
    ("Procedure",        "procedure",       8, 2_000_000),
    ("Regolamenti",      "regolamenti",     6, 2_000_000),
]


def pick(cat, n, maxsize):
    files = []
    for p in glob.glob(os.path.join(CORPUS, cat, "**", "*.pdf"), recursive=True):
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if 0 < sz <= maxsize:
            files.append((sz, p))
    files.sort()                      # i più piccoli prima (Docling più veloce)
    return [p for _, p in files[:n]]


def main():
    # 1) crea il vector store
    r = requests.post(f"{BASE}/v1/vector_stores", json={"name": "Audit ViViBanca"})
    r.raise_for_status()
    vs = r.json()["id"]
    print(f"[store] creato {vs} (Audit ViViBanca)", flush=True)

    manifest = {"vector_store_id": vs, "files": []}
    for cat, slug, n, maxsize in PLAN:
        chosen = pick(cat, n, maxsize)
        print(f"[plan] {cat}: {len(chosen)} file (slug={slug})", flush=True)
        for path in chosen:
            fn = os.path.basename(path)
            try:
                with open(path, "rb") as fh:
                    up = requests.post(f"{BASE}/v1/files",
                                       files={"file": (fn, fh, "application/pdf")},
                                       data={"purpose": "assistants"}, timeout=120)
                up.raise_for_status()
                fid = up.json()["id"]
                att = requests.post(
                    f"{BASE}/v1/vector_stores/{vs}/files",
                    json={"file_id": fid, "attributes": {
                        "sophia_directory_slug": slug,
                        "document_title": os.path.splitext(fn)[0],
                    }}, timeout=60)
                att.raise_for_status()
                manifest["files"].append({"file_id": fid, "filename": fn,
                                          "category": cat, "slug": slug})
                print(f"  + {fn[:60]}  → {fid}", flush=True)
            except Exception as e:
                print(f"  ! FALLITO {fn[:60]}: {e}", flush=True)

    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"[manifest] {len(manifest['files'])} file → {MANIFEST}", flush=True)

    # 2) polling completamento ingest (worker concurrent_jobs=1 + Docling)
    print("[ingest] attendo il completamento dei job…", flush=True)
    t0 = time.time()
    while True:
        s = requests.get(f"{BASE}/v1/vector_stores/{vs}", timeout=30).json()
        fc = s.get("file_counts", {})
        ip, done, fail = fc.get("in_progress", 0), fc.get("completed", 0), fc.get("failed", 0)
        print(f"  [{int(time.time()-t0)}s] completed={done} in_progress={ip} failed={fail}", flush=True)
        if ip == 0 and (done + fail) >= len(manifest["files"]):
            break
        if time.time() - t0 > 3600:
            print("  ! timeout 1h, interrompo il polling", flush=True)
            break
        time.sleep(15)
    print("[done] ingest concluso.", flush=True)


if __name__ == "__main__":
    main()
