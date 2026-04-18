import asyncio
import secrets
from datetime import datetime, timezone

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


