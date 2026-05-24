from typing import Dict, Any, Optional

from fastapi import File, UploadFile, HTTPException, Form, APIRouter
from fastapi.responses import StreamingResponse

from utils import get_logger
from utils.settings import MAX_FILE_SIZE
from utils.schemas import FileObject
from utils.filesystem import (
    store_file_on_disk,
    delete_file_from_disk,
    get_file_metadata,
    get_file_path,
    list_files_on_disk,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/files", tags=["Files"])


@router.post("", response_model=FileObject)
async def upload_file(file: UploadFile = File(...), purpose: str = Form("assistants")):
    """Upload a file"""
    try:
        if file.size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail=f"File too large. Max size: {MAX_FILE_SIZE} bytes")

        content = await file.read()

        metadata = await store_file_on_disk(
            content=content,
            filename=file.filename,
            content_type=file.content_type,
            purpose=purpose,
        )

        return FileObject(
            id=metadata["id"],
            bytes=metadata["bytes"],
            created_at=metadata["created_at"],
            filename=metadata["filename"],
            purpose=metadata["purpose"],
            status=metadata["status"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"File upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")


@router.get("", response_model=Dict[str, Any])
async def list_files(purpose: Optional[str] = None):
    """List uploaded files"""
    try:
        files = await list_files_on_disk(purpose)
        return {
            "object": "list",
            "data": [FileObject(**f) for f in files],
        }

    except Exception as e:
        logger.exception(f"Failed to list files: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


@router.get("/{file_id}", response_model=FileObject)
async def get_file(file_id: str):
    """Get file info"""
    try:
        if not file_id.startswith("file-"):
            raise HTTPException(status_code=404, detail="File not found")

        metadata = await get_file_metadata(file_id)
        if not metadata:
            raise HTTPException(status_code=404, detail="File not found")

        return FileObject(**metadata)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get file: {str(e)}")


@router.get("/{file_id}/content")
async def get_file_content(file_id: str, inline: bool = False):
    """Restituisce il contenuto del file.

    `inline=true` → Content-Disposition: inline, così il browser lo apre in scheda
    (utile per PDF/immagini/HTML); altrimenti attachment (download).
    """
    try:
        if not file_id.startswith("file-"):
            raise HTTPException(status_code=404, detail="File not found")

        metadata = await get_file_metadata(file_id)
        if not metadata:
            raise HTTPException(status_code=404, detail="File not found")

        file_path = await get_file_path(file_id)
        if not file_path:
            raise HTTPException(status_code=404, detail="File not found")

        def iterfile():
            with open(file_path, mode="rb") as f:
                yield from f

        disposition = "inline" if inline else "attachment"
        return StreamingResponse(
            iterfile(),
            media_type=metadata.get("content_type", "application/octet-stream"),
            headers={"Content-Disposition": f'{disposition}; filename="{metadata["filename"]}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get file content: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get file content: {str(e)}")


@router.delete("/{file_id}")
async def delete_file(file_id: str):
    """Delete a file"""
    try:
        if not file_id.startswith("file-"):
            raise HTTPException(status_code=404, detail="File not found")

        deleted = await delete_file_from_disk(file_id)

        return {"id": file_id, "object": "file", "deleted": deleted}

    except Exception as e:
        logger.exception(f"Failed to delete file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")
