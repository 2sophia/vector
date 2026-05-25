"""
Trascrizione audio/video → VTT via faster-whisper (CPU).

Stesso pattern di GLiNER (utils/entities): lazy singleton, il modello Whisper si
carica al PRIMO file audio/video e resta caldo nel processo del vector worker.
Audio → trascrizione diretta; video → ffmpeg estrae la traccia audio, poi Whisper.

Output: un file **VTT** (con timestamp), che Docling parsa nativamente → il file
audio rientra nella pipeline standard (chunk/embedding/grafo) senza codice speciale.

Le estensioni audio/video e il gating della whitelist stanno in utils/docling.py
(ASR_AUDIO_EXTENSIONS / ASR_VIDEO_EXTENSIONS, attive solo se ASR_ENABLED).
"""

import os
import logging
import subprocess

from utils.settings import (
    ASR_ENABLED,
    ASR_MODEL,
    ASR_LANGUAGE,
    ASR_DEVICE,
    ASR_COMPUTE_TYPE,
)

logger = logging.getLogger("transcribe")

# faster-whisper estrae l'audio da molti più formati di quelli che Docling
# dichiarerebbe: copriamo il set ampio (gestito da ffmpeg).
_model = None


def _get_model():
    """Carica (una volta) il modello Whisper sul device scelto. Lazy: solo al primo
    uso. Device da ASR_DEVICE ("cpu" | "cuda" | "cuda:N" | "auto"); faster-whisper
    fa la sua detection CUDA con "auto" (via CTranslate2, indipendente da torch)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        device = (ASR_DEVICE or "auto").strip().lower()
        device_index = 0
        if device.startswith("cuda:"):
            try:
                device_index = int(device.split(":", 1)[1])
            except ValueError:
                device_index = 0
            device = "cuda"
        logger.info(f"Caricamento Whisper '{ASR_MODEL}' su {device} (compute={ASR_COMPUTE_TYPE})…")
        _model = WhisperModel(
            ASR_MODEL, device=device, device_index=device_index, compute_type=ASR_COMPUTE_TYPE
        )
        logger.info(f"✅ Whisper pronto — model={ASR_MODEL} device={device} lang={ASR_LANGUAGE}")
    return _model


def warmup() -> None:
    """Pre-carica il modello se ASR è attivo. Non chiamato all'avvio worker (a
    differenza di GLiNER): l'ASR è raro, conviene caricarlo al primo file audio.
    Esposto per eventuale pre-warm esplicito/test."""
    if ASR_ENABLED:
        _get_model()


def get_duration_seconds(path: str) -> float:
    """Durata del media in secondi via ffprobe (0.0 se non leggibile)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "").strip())
    except Exception:
        return 0.0


def _extract_audio(video_path: str, out_wav: str) -> None:
    """Estrae la traccia audio di un video in WAV 16kHz mono (ottimale Whisper)."""
    r = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
         "-ar", "16000", "-ac", "1", "-y", out_wav],
        capture_output=True, text=True, timeout=1200,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: estrazione audio fallita: {(r.stderr or '')[:300]}")


def _fmt_ts(seconds: float) -> str:
    """Timestamp VTT: HH:MM:SS.mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def transcribe_to_vtt(src_path: str, out_vtt: str, is_video: bool = False) -> None:
    """Trascrive `src_path` (audio o video) e scrive un WebVTT in `out_vtt`.

    Per i video estrae prima la traccia audio con ffmpeg. Usa VAD per saltare i
    silenzi. Solleva se ffmpeg/Whisper falliscono (→ il worker manda il job FAILED).
    """
    audio_path = src_path
    tmp_wav = None
    if is_video:
        tmp_wav = out_vtt + ".extract.wav"
        _extract_audio(src_path, tmp_wav)
        audio_path = tmp_wav

    try:
        model = _get_model()
        segments, _info = model.transcribe(
            audio_path, language=ASR_LANGUAGE, beam_size=5, vad_filter=True
        )
        lines = ["WEBVTT", ""]
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
            lines.append(text)
            lines.append("")
        with open(out_vtt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
