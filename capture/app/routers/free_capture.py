import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..auth import require_token
from ..camera import camera_manager
from ..config import settings
from .. import capture_ops

router = APIRouter(prefix="/capture/free")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_camera():
    if not camera_manager.ready:
        raise HTTPException(503, "Camera not ready")


class FreeStillRequest(BaseModel):
    apply_flat_field: bool = False


class FreeVideoRequest(BaseModel):
    duration_s: int
    bitrate_bps: Optional[int] = None


@router.post("/still", dependencies=[Depends(require_token)])
async def free_still(req: FreeStillRequest = FreeStillRequest()):
    _require_camera()
    return await asyncio.to_thread(
        capture_ops.free_still, camera_manager, req.apply_flat_field
    )


@router.post("/video", dependencies=[Depends(require_token)])
async def free_video(req: FreeVideoRequest):
    _require_camera()
    bitrate = req.bitrate_bps or capture_ops.DEFAULT_BITRATE
    return await asyncio.to_thread(
        capture_ops.free_video, camera_manager, req.duration_s, bitrate
    )


@router.get("/files", dependencies=[Depends(require_token)])
async def list_freecapture_files(
    date: Optional[str] = Query(default=None),
) -> List[dict]:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")

    date_dir = capture_ops.free_base() / date
    if not date_dir.exists():
        return []

    files = []
    for f in sorted(date_dir.iterdir()):
        if f.is_file() and not f.name.startswith(".") and f.suffix not in capture_ops._SIDECAR_SUFFIXES:
            stat = f.stat()
            files.append({
                "filename": f.name,
                "date": date,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files


@router.get("/files/{date}/{filename}", dependencies=[Depends(require_token)])
async def serve_freecapture_file(
    date: str,
    filename: str,
    thumb: bool = Query(default=False),
):
    if not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format")

    base = capture_ops.free_base()
    date_dir = (base / date).resolve()

    if not str(date_dir).startswith(str(base.resolve())):
        raise HTTPException(403, "Forbidden")

    safe_name = Path(filename).name
    file_path = (date_dir / safe_name).resolve()

    if not str(file_path).startswith(str(date_dir)):
        raise HTTPException(403, "Forbidden")
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    if thumb:
        if file_path.suffix.lower() not in capture_ops._THUMB_EXTS:
            raise HTTPException(404, "Thumbnail not available for this file type")
        data = await asyncio.to_thread(capture_ops.make_thumb, file_path)
        return Response(content=data, media_type="image/jpeg")

    media_type = "image/tiff" if file_path.suffix.lower() in (".tif", ".tiff") else None
    return FileResponse(str(file_path), media_type=media_type)


@router.delete("/files/{date}/{filename}", dependencies=[Depends(require_token)])
async def delete_freecapture_file(date: str, filename: str):
    if not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format")

    base = capture_ops.free_base()
    date_dir = (base / date).resolve()

    if not str(date_dir).startswith(str(base.resolve())):
        raise HTTPException(403, "Forbidden")

    safe_name = Path(filename).name
    file_path = (date_dir / safe_name).resolve()

    if not str(file_path).startswith(str(date_dir)):
        raise HTTPException(403, "Forbidden")
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    rel_path = f"{settings.PICTURES_DIR}/{date}/{safe_name}"
    trash_path = await asyncio.to_thread(capture_ops.trash_file, file_path, rel_path)
    return {"status": "trashed", "trash_path": str(trash_path)}


# ---------------------------------------------------------------------------
# Free video routes  (/capture/free/videos/...)
# ---------------------------------------------------------------------------

@router.get("/videos", dependencies=[Depends(require_token)])
async def list_freevideo_files(
    date: Optional[str] = Query(default=None),
) -> List[dict]:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")

    date_dir = capture_ops.video_base() / date
    if not date_dir.exists():
        return []

    files = []
    for f in sorted(date_dir.iterdir()):
        if f.is_file() and not f.name.startswith(".") and f.suffix not in capture_ops._SIDECAR_SUFFIXES:
            stat = f.stat()
            files.append({
                "filename": f.name,
                "date": date,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files


@router.get("/videos/{date}/{filename}", dependencies=[Depends(require_token)])
async def serve_freevideo_file(
    date: str,
    filename: str,
    thumb: bool = Query(default=False),
):
    if not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format")

    base = capture_ops.video_base()
    date_dir = (base / date).resolve()

    if not str(date_dir).startswith(str(base.resolve())):
        raise HTTPException(403, "Forbidden")

    safe_name = Path(filename).name
    file_path = (date_dir / safe_name).resolve()

    if not str(file_path).startswith(str(date_dir)):
        raise HTTPException(403, "Forbidden")
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    if thumb:
        if file_path.suffix.lower() not in capture_ops._THUMB_EXTS:
            raise HTTPException(404, "Thumbnail not available for this file type")
        data = await asyncio.to_thread(capture_ops.make_thumb, file_path)
        return Response(content=data, media_type="image/jpeg")

    return FileResponse(str(file_path))


@router.delete("/videos/{date}/{filename}", dependencies=[Depends(require_token)])
async def delete_freevideo_file(date: str, filename: str):
    if not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format")

    base = capture_ops.video_base()
    date_dir = (base / date).resolve()

    if not str(date_dir).startswith(str(base.resolve())):
        raise HTTPException(403, "Forbidden")

    safe_name = Path(filename).name
    file_path = (date_dir / safe_name).resolve()

    if not str(file_path).startswith(str(date_dir)):
        raise HTTPException(403, "Forbidden")
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    rel_path = f"{settings.VIDEOS_DIR}/{date}/{safe_name}"
    trash_path = await asyncio.to_thread(capture_ops.trash_file, file_path, rel_path)
    return {"status": "trashed", "trash_path": str(trash_path)}
