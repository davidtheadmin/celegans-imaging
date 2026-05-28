import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_token
from ..camera import camera_manager

router = APIRouter(prefix="/camera")


class EvBias(BaseModel):
    value: float


class CalibrationUpsert(BaseModel):
    label: str
    fov_cm: float


class CalibrationActive(BaseModel):
    label: str


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


@router.get("/ev", dependencies=[Depends(require_token)])
async def get_ev():
    return {"value": camera_manager.get_ev_bias()}


@router.post("/ev", dependencies=[Depends(require_token)])
async def set_ev(body: EvBias):
    clamped = await asyncio.to_thread(camera_manager.set_ev_bias, body.value)
    return {"value": clamped}


# ── Spatial calibration ────────────────────────────────────────────────────────
# Metadata-only; these endpoints do not require the camera hardware to be ready.

@router.get("/calibration", dependencies=[Depends(require_token)])
async def get_calibration():
    return camera_manager.get_calibrations()


@router.post("/calibration", dependencies=[Depends(require_token)])
async def upsert_calibration(body: CalibrationUpsert):
    if body.fov_cm <= 0:
        raise HTTPException(400, "fov_cm must be positive")
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "label is required")
    return await asyncio.to_thread(camera_manager.upsert_calibration, label, body.fov_cm)


@router.post("/calibration/active", dependencies=[Depends(require_token)])
async def set_active_calibration(body: CalibrationActive):
    return await asyncio.to_thread(camera_manager.set_active_calibration, body.label)


@router.delete("/calibration/{label}", dependencies=[Depends(require_token)])
async def delete_calibration(label: str):
    return await asyncio.to_thread(camera_manager.delete_calibration, label)
