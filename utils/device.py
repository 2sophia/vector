"""
Risoluzione del device (CPU/GPU) per i modelli che girano IN-PROCESS nel vector
worker: GLiNER (entità) e faster-whisper (ASR). I servizi esterni — BGE-M3
embeddings e Docling parser — hanno il loro device nei rispettivi container e NON
sono toccati da qui.

Perché la scelta è utile: il worker spesso gira sullo stesso host dove la GPU è
già occupata da vLLM/embeddings/parser. Lì conviene tenere GLiNER/ASR su CPU (sono
piccoli) e lasciare la VRAM ai servizi grossi. Su un host con GPU libera, invece,
spostarli su GPU accelera. Da qui le manopole per-modello SOPHIA_VECTOR_*_DEVICE.

Valori accettati: "cpu", "cuda", "cuda:N", "auto" (GPU se disponibile, altrimenti
CPU). La risoluzione è best-effort: se CUDA è richiesto ma non disponibile si
ricade su CPU con un warning, senza rompere l'ingestion.
"""

from utils.logger import get_logger

logger = get_logger(__name__)


def cuda_available() -> bool:
    """True se torch vede almeno una GPU CUDA. Tollerante: torch assente → False."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_device(pref: str, *, what: str = "model") -> str:
    """Normalizza la preferenza in un device concreto ("cpu" o "cuda[:N]").

    - "cpu"           → "cpu"
    - "auto"/""       → "cuda" se disponibile, altrimenti "cpu"
    - "cuda"/"cuda:N" → quello richiesto se CUDA c'è, altrimenti "cpu" (warning)
    """
    pref = (pref or "auto").strip().lower()
    if pref == "cpu":
        return "cpu"
    if pref in ("auto", ""):
        return "cuda" if cuda_available() else "cpu"
    if pref.startswith("cuda"):
        if cuda_available():
            return pref
        logger.warning(f"{what}: device '{pref}' richiesto ma CUDA non disponibile → CPU")
        return "cpu"
    logger.warning(f"{what}: device '{pref}' non riconosciuto → CPU")
    return "cpu"
