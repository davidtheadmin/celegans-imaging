import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..camera import camera_manager
from .. import capture_ops
from .. import sessions as session_store

router = APIRouter()


def _require_camera():
    if not camera_manager.ready:
        raise HTTPException(503, "Camera not ready")


class PlateCapRequest(BaseModel):
    # motility fields
    duration_s: Optional[int] = None
    bitrate_bps: Optional[int] = None
    # survival fields
    quadrant: Optional[str] = None
    apply_flat_field: bool = False


@router.post(
    "/sessions/{session_id}/plates/{plate_id}/capture",
    dependencies=[Depends(require_token)],
)
async def capture_plate(session_id: str, plate_id: str, req: PlateCapRequest = PlateCapRequest()):
    _require_camera()
    session, plate = session_store.get_plate(session_id, plate_id)
    plate_dir = session_store.get_plate_dir(session_id, plate.folder_name)

    if session.assay_mode == "motility":
        duration = req.duration_s or int(session.assay_config.get("duration_s", capture_ops.DEFAULT_DURATION))
        bitrate = req.bitrate_bps or int(session.assay_config.get("bitrate_bps", capture_ops.DEFAULT_BITRATE))
        return await asyncio.to_thread(
            capture_ops.plate_motility, camera_manager, plate_dir, duration, bitrate
        )

    # survival
    if session.assay_config.get("quadrants") and not req.quadrant:
        raise HTTPException(400, "quadrant is required when assay_config.quadrants is true")
    return await asyncio.to_thread(
        capture_ops.plate_survival,
        camera_manager,
        plate_dir,
        req.quadrant,
        req.apply_flat_field,
    )


@router.get(
    "/sessions/{session_id}/plates/{plate_id}/files",
    dependencies=[Depends(require_token)],
)
async def list_plate_files(session_id: str, plate_id: str) -> List[dict]:
    session, plate = session_store.get_plate(session_id, plate_id)
    plate_dir = session_store.get_plate_dir(session_id, plate.folder_name)

    if not plate_dir.exists():
        return []

    files = []
    for f in sorted(plate_dir.iterdir()):
        if f.is_file():
            stat = f.stat()
            files.append({
                "filename": f.name,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files
