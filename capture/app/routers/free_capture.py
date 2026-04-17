import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..auth import require_token
from ..camera import camera_manager
from .. import capture_ops

router = APIRouter(prefix="/capture/free")


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
