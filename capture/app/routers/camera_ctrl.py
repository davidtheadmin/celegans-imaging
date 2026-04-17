import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_token
from ..camera import camera_manager

router = APIRouter(prefix="/camera")


def _require_camera():
    if not camera_manager.ready:
        raise HTTPException(503, "Camera not ready")


@router.post("/ae/lock", dependencies=[Depends(require_token)])
async def ae_lock():
    _require_camera()
    return await asyncio.to_thread(camera_manager.lock_ae)


@router.post("/ae/unlock", dependencies=[Depends(require_token)])
async def ae_unlock():
    _require_camera()
    await asyncio.to_thread(camera_manager.unlock_ae)
    return {"locked": False}


@router.get("/exposure", dependencies=[Depends(require_token)])
async def exposure_state():
    return camera_manager.get_exposure_state()
