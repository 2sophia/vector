"""
Benchmark A/B di modelli NER (GLiNER) sugli STESSI chunk reali.

Sfrutta `models.NerModel` (generico → ogni modello è solo una stringa): istanzia
ciascun candidato, estrae entità sugli stessi testi, e riporta volume + risorse.

⚠️ Senza un gold set annotato questo NON misura la QUALITÀ (precision/recall):
misura **quante** entità escono, **quanto** costa (tempo, VRAM di picco) e mostra
un **campione** di entità per il giudizio qualitativo manuale — fondamentale per
valutare l'italiano (es. "Banca d'Italia" splittato? codici norma riconosciuti?).

Uso:
  .venv/bin/python scripts/bench_ner.py <vector_store_id> [--n 50] [--device cpu|cuda] \
      [--models id1,id2,...] [--sample 10]

Esempio (confronto attuale vs RetriCo vs un multilingua):
  .venv/bin/python scripts/bench_ner.py vs_1aa2dcc4fa18 --n 80 --device cuda
"""

import argparse
import time
from typing import List

# Candidati di default. Aggiungi/togli da CLI con --models.
DEFAULT_MODELS = [
    "urchade/gliner_multi-v2.1",                  # ATTUALE — mDeBERTa multilingua (base)
    "knowledgator/gliner-multitask-large-v0.5",   # RetriCo — large (EN-centric, da verificare su IT)
]


def _reset_peak(device: str) -> None:
    if device.startswith("cuda"):
        try:
            import torch
            torch.cuda.reset_peak_memory_stats()
        except Exception:
            pass


def _vram_peak_mb(device: str) -> float:
    if device.startswith("cuda"):
        try:
            import torch
            return round(torch.cuda.max_memory_allocated() / 1e6, 1)
        except Exception:
            return 0.0
    return 0.0


def _free(model) -> None:
    """Libera il modello dalla GPU/RAM prima del candidato successivo."""
    try:
        model._model = None
        import gc
        gc.collect()
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass


def load_texts(vsid: str, n: int) -> List[str]:
    from utils.qdrant import qdrant_client
    pts, _ = qdrant_client.scroll(
        collection_name=vsid, limit=n, with_payload=True, with_vectors=False
    )
    out: List[str] = []
    for p in pts:
        t = (p.payload or {}).get("text")
        if t:
            out.append(t)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("vector_store_id")
    ap.add_argument("--n", type=int, default=50, help="numero di chunk")
    ap.add_argument("--device", default="cpu", help="cpu | cuda | cuda:N")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--sample", type=int, default=10, help="entità di esempio da mostrare")
    args = ap.parse_args()

    from models.ner import NerModel

    texts = load_texts(args.vector_store_id, args.n)
    if not texts:
        print("Nessun chunk con testo nel vector store.")
        return
    print(f"Benchmark NER su {len(texts)} chunk · device={args.device}\n")

    rows = []
    for model_id in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"── {model_id} ──")
        m = NerModel(model_id, args.device, "bench")
        if m.load() is None:
            print("  load fallito, skip\n")
            continue
        _reset_peak(m.device)
        t0 = time.time()
        results = m.extract(texts, None)  # usa GLINER_LABELS di default
        dt = round(time.time() - t0, 2)
        total = sum(len(r) for r in results)
        distinct = len({(e["type"], e["normalized_name"]) for r in results for e in r})
        vram = _vram_peak_mb(m.device)
        rows.append((model_id, total, distinct, dt, vram))
        sample = [f'{e["name"]}({e["type"]})' for r in results for e in r][: args.sample]
        print(f"  entità: {total} (distinte {distinct}) · {dt}s · VRAM picco {vram}MB · device {m.device}")
        print(f"  esempi: {sample}\n")
        _free(m)

    print("=== RIEPILOGO ===")
    print(f"{'model':45} {'ent':>6} {'distinct':>9} {'sec':>7} {'VRAM_MB':>9}")
    for r in rows:
        print(f"{r[0]:45} {r[1]:>6} {r[2]:>9} {r[3]:>7} {r[4]:>9}")
    print("\nGiudica la QUALITÀ dai campioni (entità IT sensate? codici/norme presi? "
          "nomi non spezzati?), non solo dal volume.")


if __name__ == "__main__":
    main()
