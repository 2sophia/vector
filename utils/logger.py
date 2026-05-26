"""Logging configuration"""

import logging
import os
import warnings

# Rumore di huggingface_hub allo startup: la progress bar "Fetching N files: 100%|…"
# e il UserWarning ripetuto su `resume_download` (emesso a ogni snapshot_download).
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
warnings.filterwarnings("ignore", message=r".*resume_download.*")

# DEBUG solo se richiesto esplicitamente (SOPHIA_VECTOR_DEBUG=true); altrimenti INFO.
# A DEBUG il root logger faceva sputare a httpcore/httpx/asyncio/huggingface_hub
# OGNI richiesta HTTP (header inclusi) → log illeggibile.
_DEBUG = os.getenv("SOPHIA_VECTOR_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")

logging.basicConfig(
    level=logging.DEBUG if _DEBUG else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)

# Librerie di terze parti troppo rumorose: tenute a WARNING (loggano solo problemi
# reali) così i log dell'app restano leggibili anche in DEBUG.
for _noisy in (
    "httpcore", "httpx", "urllib3", "asyncio",
    "huggingface_hub", "filelock", "transformers", "sentence_transformers",
    "gliner", "gliner.model",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# pymongo: ancora più silenzioso
logging.getLogger("pymongo").setLevel(logging.WARNING)
logging.getLogger("pymongo.topology").setLevel(logging.ERROR)
logging.getLogger("pymongo.connection").setLevel(logging.ERROR)
logging.getLogger("pymongo.pool").setLevel(logging.ERROR)
logging.getLogger("pymongo.server").setLevel(logging.ERROR)
logging.getLogger("pymongo.heartbeat").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance"""
    return logging.getLogger(name)
