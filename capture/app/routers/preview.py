import asyncio
import secrets
from datetime import datetime, timezone

import cv2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse

from ..auth import require_token
from ..camera import camera_manager
from ..config import settings
from ..focus import compute_focus_score

router = APIRouter()

_BOUNDARY = b"frame"


def _require_camera():
    if not camera_manager.ready:
        raise HTTPException(503, "Camera not ready")


async def _mjpeg_stream():
    last = None
    while True:
        jpeg = camera_manager.get_latest_jpeg()
        if jpeg is not None and jpeg is not last:
            last = jpeg
            yield (
                b"--" + _BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                b"\r\n" + jpeg + b"\r\n"
            )
        else:
            await asyncio.sleep(1 / 15)


@router.get("/preview.mjpg")
async def preview_stream(token: str = ""):
    # Auth via query param — browser <img src> cannot send custom headers.
    if not token or not secrets.compare_digest(token, settings.TOKEN):
        raise HTTPException(401, "Unauthorized")
    _require_camera()
    return StreamingResponse(
        _mjpeg_stream(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY.decode()}",
    )


@router.get("/focus", dependencies=[Depends(require_token)])
async def focus():
    _require_camera()
    lores = camera_manager.get_latest_lores()
    if lores is None:
        raise HTTPException(503, "No preview frame available yet")
    score = compute_focus_score(lores)
    return {"score": score, "at": datetime.now(timezone.utc).isoformat()}


def _lores_magnifier() -> bytes:
    """Crop center of the lores preview frame — no camera acquisition needed."""
    lores = camera_manager.get_latest_lores()  # RGB uint8, 1280×960
    if lores is None:
        raise HTTPException(503, "No preview frame available")
    h, w = lores.shape[:2]
    crop_w, crop_h = w // 4, h // 4  # 320×240 — zoomed center region
    cy, cx = h // 2, w // 2
    crop = lores[cy - crop_h // 2: cy + crop_h // 2, cx - crop_w // 2: cx + crop_w // 2]
    bgr = crop[:, :, ::-1].copy()
    scaled = cv2.resize(bgr, (600, 450), interpolation=cv2.INTER_LINEAR)
    ok, buf = cv2.imencode(".jpg", scaled, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise HTTPException(500, "Encode failed")
    return buf.tobytes()


@router.get("/magnifier.jpg", dependencies=[Depends(require_token)])
async def magnifier():
    _require_camera()
    data = await asyncio.to_thread(_lores_magnifier)
    return Response(content=data, media_type="image/jpeg")
