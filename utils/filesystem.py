# filepath: utils/filesystem.py

import asyncio
import hashlib
import json
import os
import glob
from typing import Callable, Optional

import aiofiles
from slugify import slugify

from utils import FILES_STORAGE, get_timestamp, generate_id


def _sha256_file(path: str) -> str:
    """sha256 del contenuto del file, letto a blocchi (no full buffer in RAM)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _find_file_path(file_id: str) -> str | None:
    """Trova il file su disco (con qualsiasi estensione)."""
    pattern = os.path.join(FILES_STORAGE, f"{file_id}.*")
    matches = [f for f in glob.glob(pattern) if not f.endswith("_metadata.json")]
    return matches[0] if matches else None


async def get_file_metadata(file_id: str) -> dict | None:
    """Legge i metadata di un file."""
    metadata_path = os.path.join(FILES_STORAGE, f"{file_id}_metadata.json")
    if not os.path.exists(metadata_path):
        return None

    async with aiofiles.open(metadata_path, "r") as f:
        return json.loads(await f.read())


async def get_file_path(file_id: str) -> str | None:
    """Ritorna il path del file su disco."""
    metadata = await get_file_metadata(file_id)
    if metadata and metadata.get("path"):
        path = metadata["path"]
        if os.path.exists(path):
            return path

    # Fallback: cerca con glob
    return _find_file_path(file_id)


async def delete_file_from_disk(file_id: str) -> bool:
    """Cancella file e metadata dal disco. Ritorna True se cancellato."""
    file_path = await get_file_path(file_id)
    metadata_path = os.path.join(FILES_STORAGE, f"{file_id}_metadata.json")

    deleted = False

    if file_path and os.path.exists(file_path):
        os.remove(file_path)
        deleted = True

    if os.path.exists(metadata_path):
        os.remove(metadata_path)
        deleted = True

    return deleted


async def store_file_on_disk(
    content: Optional[bytes] = None,
    filename: str = "",
    content_type: str | None = None,
    purpose: str = "assistants",
    stream_source: Optional[Callable[[str], int]] = None,
) -> dict:
    """Salva un file su disco + metadata JSON.

    Due modalità mutuamente esclusive:
    - content=bytes        → scrittura standard di un buffer in memoria
    - stream_source=callable(path) → la callable scrive direttamente sul path,
      utile per download in streaming senza tenere il contenuto in RAM.
      La callable deve ritornare il numero di byte scritti.
    """
    if (content is None) == (stream_source is None):
        raise ValueError("provide exactly one of: content, stream_source")

    file_id = generate_id("file-")

    _, ext = os.path.splitext(filename)
    file_path = os.path.join(FILES_STORAGE, f"{file_id}{ext}")

    if stream_source is not None:
        bytes_written = await asyncio.to_thread(stream_source, file_path)
    else:
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
        bytes_written = len(content)

    name, _ = os.path.splitext(filename)
    safe_name = slugify(name, max_length=200, word_boundary=True)
    safe_filename = f"{safe_name}{ext}" if ext else safe_name

    # Hash del contenuto: usato per la dedup (stesso contenuto = stesso hash).
    content_hash = await asyncio.to_thread(_sha256_file, file_path)

    file_metadata = {
        "id": file_id,
        "filename": safe_filename,
        "original_filename": filename,
        "bytes": bytes_written,
        "content_hash": content_hash,
        "purpose": purpose,
        "created_at": get_timestamp(),
        "content_type": content_type or "application/octet-stream",
        "status": "uploaded",
        "path": file_path,
    }

    metadata_path = os.path.join(FILES_STORAGE, f"{file_id}_metadata.json")
    async with aiofiles.open(metadata_path, "w") as f:
        await f.write(json.dumps(file_metadata))

    return file_metadata


async def list_files_on_disk(purpose: str | None = None) -> list[dict]:
    """Lista tutti i file su disco, opzionalmente filtrati per purpose."""
    files = []

    for filename in os.listdir(FILES_STORAGE):
        if filename.endswith("_metadata.json") and filename.startswith("file-"):
            try:
                async with aiofiles.open(os.path.join(FILES_STORAGE, filename), "r") as f:
                    metadata = json.loads(await f.read())

                if purpose is None or metadata.get("purpose") == purpose:
                    files.append(metadata)
            except Exception:
                continue

    files.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return files