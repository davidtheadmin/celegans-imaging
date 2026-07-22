"""
analyze.py — "Analyze on laptop" job hand-off.

The web-UI button captures a full-res frame on the Pi and parks it in a single
job slot. The laptop's launcher long-polls GET /analyze/next, grabs the frame,
and runs the staging model locally. Transport is long-poll only, so the laptop
stays a pure HTTP client with no inbound listener.

Single slot by design: a second press replaces the frame still waiting, so the
launcher never processes a stale capture. The slot holds exactly one job; any
job id that is not the current slot reads as "gone".
"""
import asyncio
import io
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image

from ..auth import require_token
from ..camera import camera_manager

router = APIRouter(prefix="/analyze")

# Single-slot holder guarded by an asyncio.Condition. _slot is either None or
# {"id": str, "state": "pending"|"taken", "data": bytes|None}. Frame bytes are
# dropped once taken; the id+state linger so status queries can still answer.
_cond = asyncio.Condition()
_slot: dict | None = None

_NEXT_TIMEOUT_S = 25  # long-poll idle timeout; client re-polls after a 204


def _require_camera() -> None:
    if not camera_manager.ready:
        raise HTTPException(503, "Camera not ready")


def _encode_tiff(arr) -> bytes:
    """LZW-compressed TIFF bytes, matching capture_ops.save_still's on-disk
    format but kept in memory (the frame is never written to Pi disk)."""
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="TIFF", compression="tiff_lzw")
    return buf.getvalue()


@router.post("", dependencies=[Depends(require_token)])
async def analyze():
    """Capture a full-res frame and park it in the slot, replacing any frame
    still waiting. Returns the new job id for the web UI to poll."""
    _require_camera()
    arr = await asyncio.to_thread(camera_manager.capture_still)  # 409 if recording
    data = await asyncio.to_thread(_encode_tiff, arr)
    job_id = secrets.token_hex(8)
    global _slot
    async with _cond:
        _slot = {"id": job_id, "state": "pending", "data": data}
        _cond.notify_all()
    return {"job_id": job_id}


@router.get("/next", dependencies=[Depends(require_token)])
async def analyze_next():
    """Long-poll for the pending frame. Returns the TIFF bytes with an X-Job-Id
    header and marks the job taken, or 204 if none arrives within the timeout."""
    global _slot
    async with _cond:
        try:
            await asyncio.wait_for(
                _cond.wait_for(
                    lambda: _slot is not None and _slot["state"] == "pending"
                ),
                timeout=_NEXT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return Response(status_code=204)
        _slot["state"] = "taken"
        data = _slot["data"]
        _slot["data"] = None  # free the frame; id+state stay for status queries
        job_id = _slot["id"]
    return Response(
        content=data, media_type="image/tiff", headers={"X-Job-Id": job_id}
    )


@router.get("/status/{jid}", dependencies=[Depends(require_token)])
async def analyze_status(jid: str):
    """pending (waiting for the laptop), taken (laptop grabbed it), or gone
    (never existed, replaced by a newer press, or cancelled)."""
    async with _cond:
        if _slot is not None and _slot["id"] == jid:
            return {"state": _slot["state"]}
    return {"state": "gone"}


@router.post("/cancel/{jid}", dependencies=[Depends(require_token)])
async def analyze_cancel(jid: str):
    """Drop the parked frame if it is still this job. Idempotent."""
    global _slot
    async with _cond:
        if _slot is not None and _slot["id"] == jid:
            _slot = None
    return {"status": "cancelled"}
