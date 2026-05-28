"""
NLP utility endpoints (/v1/nlp/*).

Espongono come API on-demand i modelli già presenti nel codebase: tokenizer BGE-M3,
GLiNER (NER), GliClass (classify), GLiNER-relex (relazioni), Whisper (transcribe).

LAZY: ogni modello si carica al primo hit del suo endpoint → costo zero se non usati.
Le istanze qui sono DEDICATE con enabled=True: l'esposizione è governata da NLP_ENABLED,
NON dai flag di ingestion (GLINER_ENABLED…), così gli endpoint funzionano anche con
l'estrazione-in-ingestion spenta. Queste istanze sono le UNICHE a caricare i pesi nel
backend: le usano sia gli endpoint sia il worker (che le chiama via HTTP) → una copia sola.
"""

import os
import shutil
import asyncio
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from utils import get_logger
from utils.config import settings
from utils import tokenizer as tok
from utils.docling import ASR_AUDIO_EXTENSIONS, ASR_VIDEO_EXTENSIONS
from models.ner import NerModel
from models.relex import RelexModel
from models.classifier import ClassifierModel

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/nlp", tags=["NLP"])

# Device dei modelli GLiNER-family: uno solo (sono una copia condivisa endpoint+ingestion).
_DEVICE = settings.GLINER_DEVICE

# Istanze DEDICATE agli endpoint (enabled=True → load on-demand a prescindere dai flag di
# ingestion). Nel processo backend sono le uniche a caricare i pesi: il registry globale
# qui non fa warmup. Lazy: nessun costo finché non arriva la prima richiesta.
_ner = NerModel(settings.GLINER_MODEL, _DEVICE, "GLiNER", enabled=True)
_relex = RelexModel(settings.RELATIONS_MODEL, _DEVICE, "GLiNER-relex", enabled=True)
_classifier = ClassifierModel(settings.CLASSIFIER_MODEL, _DEVICE, "GliClass", enabled=True)


# --------------------------- request models ---------------------------

class TokenizeRequest(BaseModel):
    text: str
    add_special_tokens: bool = False


class DetokenizeRequest(BaseModel):
    token_ids: List[int]
    skip_special_tokens: bool = True


class NerRequest(BaseModel):
    texts: List[str]
    labels: Optional[List[str]] = None  # None → default GLINER_LABELS


class ClassifyRequest(BaseModel):
    texts: List[str]
    labels: Optional[List[str]] = None  # None → default CLASSIFIER_LABELS
    threshold: Optional[float] = None


class RelexRequest(BaseModel):
    texts: List[str]
    entity_labels: Optional[List[str]] = None
    relation_labels: Optional[List[str]] = None


# --------------------------- tokenizer (leggero) ---------------------------

@router.post("/tokenize")
async def nlp_tokenize(body: TokenizeRequest):
    """Tokenizza con il tokenizer BGE-M3. Ritorna {token_ids, tokens, count}.
    `count` è quello che serve per dimensionare chunk_max_tokens."""
    try:
        return await asyncio.to_thread(tok.tokenize, body.text, body.add_special_tokens)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/detokenize")
async def nlp_detokenize(body: DetokenizeRequest):
    """Ricostruisce il testo da una lista di token id."""
    try:
        return await asyncio.to_thread(tok.detokenize, body.token_ids, body.skip_special_tokens)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --------------------------- modelli zero-shot ---------------------------

@router.post("/ner")
async def nlp_ner(body: NerRequest):
    """Estrazione entità zero-shot (GLiNER). `labels` opzionale (default = schema globale).
    Ritorna una lista allineata a `texts`."""
    if await asyncio.to_thread(_ner.load) is None:
        raise HTTPException(status_code=503, detail="GLiNER non disponibile su questo deployment")
    results = await asyncio.to_thread(_ner.extract, body.texts, body.labels)
    return {"object": "nlp.ner", "results": results}


@router.post("/classify")
async def nlp_classify(body: ClassifyRequest):
    """Classificazione zero-shot (GliClass). Ritorna per ogni testo [{label, score}]."""
    if await asyncio.to_thread(_classifier.load) is None:
        raise HTTPException(
            status_code=503, detail="Classifier (GliClass) non disponibile — `pip install gliclass`"
        )
    results = await asyncio.to_thread(_classifier.classify, body.texts, body.labels, body.threshold)
    return {"object": "nlp.classify", "results": results}


@router.post("/relex")
async def nlp_relex(body: RelexRequest):
    """Estrazione relazioni tipizzate zero-shot (GLiNER-relex)."""
    if await asyncio.to_thread(_relex.load) is None:
        raise HTTPException(status_code=503, detail="GLiNER-relex non disponibile su questo deployment")
    results = await asyncio.to_thread(_relex.extract, body.texts, body.entity_labels, body.relation_labels)
    return {"object": "nlp.relex", "results": results}


# --------------------------- transcribe (ASR) ---------------------------

@router.post("/transcribe")
async def nlp_transcribe(file: UploadFile = File(...)):
    """Trascrizione audio/video (Whisper). Ritorna {vtt, text, language}."""
    if not settings.ASR_ENABLED:
        raise HTTPException(status_code=503, detail="ASR disabilitato (SOPHIA_VECTOR_ASR_ENABLED)")
    ext = os.path.splitext(file.filename or "")[1].lower()
    is_audio = ext in ASR_AUDIO_EXTENSIONS
    is_video = ext in ASR_VIDEO_EXTENSIONS
    if not (is_audio or is_video):
        raise HTTPException(status_code=415, detail=f"Estensione non audio/video: '{ext}'")
    content = await file.read()

    def _run() -> Dict[str, Any]:
        from utils.transcribe import transcribe_to_vtt
        tmpdir = tempfile.mkdtemp(prefix="nlp_asr_")
        src = os.path.join(tmpdir, f"in{ext}")
        out_vtt = os.path.join(tmpdir, "out.vtt")
        try:
            with open(src, "wb") as f:
                f.write(content)
            transcribe_to_vtt(src, out_vtt, is_video=is_video)
            with open(out_vtt, "r", encoding="utf-8") as f:
                vtt = f.read()
            # testo "piatto": scarta header WEBVTT, timestamp e righe vuote
            text_lines = [
                s for line in vtt.splitlines()
                if (s := line.strip()) and s != "WEBVTT" and "-->" not in s
            ]
            return {"vtt": vtt, "text": " ".join(text_lines), "language": settings.ASR_LANGUAGE}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception(f"nlp transcribe failed: {e}")
        raise HTTPException(status_code=500, detail="Transcription failed (vedi log del server)")
