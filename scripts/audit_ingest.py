#!/usr/bin/env python3
"""Audit ingest — carica un campione di documenti via le API REST del backend (NON una
pipeline custom: stesso percorso del prodotto, così l'eval è reale).

Per ogni sottocartella del corpus seleziona i PDF più piccoli (Docling è più veloce),
li uploada (/v1/files) e li attacca a un vector store (/v1/vector_stores/{id}/files)
usando il nome della sottocartella come slug. Salva un manifest (gitignorato) per il
gold set.

Configura il corpus via env (nessun path hardcoded — repo pubblica):

    AUDIT_CORPUS=/path/al/tuo/corpus \
    AUDIT_PER_DIR=8 .venv/bin/python scripts/audit_ingest.py
"""
import os, json, time, glob
import requests

BASE = os.environ.get("AUDIT_BACKEND", "http://127.0.0.1:8100")
CORPUS = os.environ.get("AUDIT_CORPUS", "")            # <-- imposta via env, niente path nel codice
PER_DIR = int(os.environ.get("AUDIT_PER_DIR", "8"))
MAX_BYTES = int(os.environ.get("AUDIT_MAX_BYTES", "2000000"))
STORE_NAME = os.environ.get("AUDIT_STORE_NAME", "Audit sample")
MANIFEST = os.path.join(os.path.dirname(__file__), "audit_manifest.json")  # gitignorato


def slugify(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pick(dir_path, n, maxsize):
    files = []
    for p in glob.glob(os.path.join(dir_path, "**", "*.pdf"), recursive=True):
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if 0 < sz <= maxsize:
            files.append((sz, p))
    files.sort()
    return [p for _, p in files[:n]]


def main():
    if not CORPUS or not os.path.isdir(CORPUS):
        raise SystemExit("Imposta AUDIT_CORPUS a una cartella di documenti (env). "
                         "Niente path hardcoded: questa repo è pubblica.")

    r = requests.post(f"{BASE}/v1/vector_stores", json={"name": STORE_NAME})
    r.raise_for_status()
    vs = r.json()["id"]
    print(f"[store] creato {vs} ({STORE_NAME})", flush=True)

    subdirs = sorted(d for d in glob.glob(os.path.join(CORPUS, "*")) if os.path.isdir(d))
    if not subdirs:
        subdirs = [CORPUS]

    manifest = {"vector_store_id": vs, "files": []}
    for d in subdirs:
        slug = slugify(os.path.basename(d.rstrip("/")) or "root")
        chosen = pick(d, PER_DIR, MAX_BYTES)
        if not chosen:
            continue
        print(f"[plan] {os.path.basename(d)}: {len(chosen)} file (slug={slug})", flush=True)
        for path in chosen:
            fn = os.path.basename(path)
            try:
                with open(path, "rb") as fh:
                    up = requests.post(f"{BASE}/v1/files",
                                       files={"file": (fn, fh, "application/pdf")},
                                       data={"purpose": "assistants"}, timeout=120)
                up.raise_for_status()
                fid = up.json()["id"]
                requests.post(f"{BASE}/v1/vector_stores/{vs}/files",
                              json={"file_id": fid, "attributes": {
                                  "sophia_directory_slug": slug,
                                  "document_title": os.path.splitext(fn)[0],
                              }}, timeout=60).raise_for_status()
                manifest["files"].append({"file_id": fid, "filename": fn, "slug": slug})
                print(f"  + {fn[:60]}  → {fid}", flush=True)
            except Exception as e:
                print(f"  ! FALLITO {fn[:60]}: {e}", flush=True)

    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"[manifest] {len(manifest['files'])} file → {MANIFEST} (gitignorato)", flush=True)

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
            print("  ! timeout 1h", flush=True)
            break
        time.sleep(15)
    print("[done] ingest concluso.", flush=True)


if __name__ == "__main__":
    main()
