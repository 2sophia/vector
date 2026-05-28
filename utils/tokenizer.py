"""
Tokenizer BGE-M3 esposto come utility (endpoint /v1/nlp/tokenize|detokenize).

È lo STESSO tokenizer usato per il chunking (PARSER_MODEL_TOKENIZER): contare/ispezionare
i token aiuta a scegliere chunk_max_tokens. Lazy singleton, leggero (CPU, ~MB): si carica
al primo uso e resta caldo nel processo. Best-effort sull'import di transformers.
"""

from typing import Any, Dict, List

from utils.logger import get_logger
from utils.settings import PARSER_MODEL_TOKENIZER

logger = get_logger(__name__)

_tokenizer = None
_failed = False


def _get_tokenizer():
    """Carica una volta il tokenizer e lo riusa. None se transformers/modello assenti."""
    global _tokenizer, _failed
    if _tokenizer is not None or _failed:
        return _tokenizer
    try:
        from transformers import AutoTokenizer

        logger.info(f"Caricamento tokenizer '{PARSER_MODEL_TOKENIZER}' (one-time)…")
        _tokenizer = AutoTokenizer.from_pretrained(PARSER_MODEL_TOKENIZER)
        return _tokenizer
    except Exception as e:
        _failed = True
        logger.warning(f"Tokenizer non disponibile: {e}")
        return None


def available() -> bool:
    return _get_tokenizer() is not None


def tokenize(text: str, add_special_tokens: bool = False) -> Dict[str, Any]:
    """Tokenizza un testo. Ritorna gli id, i token (subword) e il conteggio.
    Il `count` è quello che conta per chunk_max_tokens (niente bisogno di /count-tokens)."""
    tok = _get_tokenizer()
    if tok is None:
        raise RuntimeError("tokenizer non disponibile")
    ids: List[int] = tok.encode(text or "", add_special_tokens=add_special_tokens)
    pieces: List[str] = tok.convert_ids_to_tokens(ids)
    return {"token_ids": ids, "tokens": pieces, "count": len(ids)}


def detokenize(token_ids: List[int], skip_special_tokens: bool = True) -> Dict[str, Any]:
    """Ricostruisce il testo da una lista di token id."""
    tok = _get_tokenizer()
    if tok is None:
        raise RuntimeError("tokenizer non disponibile")
    text = tok.decode(token_ids or [], skip_special_tokens=skip_special_tokens)
    return {"text": text}
