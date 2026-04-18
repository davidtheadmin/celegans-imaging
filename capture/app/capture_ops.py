import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import HTTPException
from PIL import Image

from .config import settings

log = logging.getLogger(__name__)

# capture.py lives at capture/capture.py (sibling of this package's parent dir).
# WorkingDirectory for the service is capture/, so parents[1] = capture/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture import apply_flat_field  # noqa: E402
from capture import load_master_flat as _load_master_flat  # noqa: E402

DEFAULT_BITRATE = 25_000_000
DEFAULT_DURATION = 30


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

def _flat_dir() -> Path:
    return Path(settings.DATA_ROOT) / "flatfield"


def _free_dir() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    d = Path(settings.DATA_ROOT) / "freecapture" / today
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# ------------------------------------------------------------------
# Flat-field
# ------------------------------------------------------------------

def load_flat() -> np.ndarray:
    try:
        return _load_master_flat(ff_dir=_flat_dir())
    except FileNotFoundError:
        raise HTTPException(
            400,
            "No master flat found. Run on the Pi: "
            "python3 capture/capture.py --capture-flat",
        )


# ------------------------------------------------------------------
# Saving
# ------------------------------------------------------------------

def save_jpeg(arr: np.ndarray, path: Path, quality: int = 90) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, "RGB").save(path, format="JPEG", quality=quality)


# ------------------------------------------------------------------
# ffmpeg wrapping
# ------------------------------------------------------------------

def wrap_h264(h264_path: Path) -> Path:
    """Try to remux .h264 -> .mp4 with ffmpeg. Falls back to raw .h264."""
    mp4_path = h264_path.with_suffix(".mp4")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-framerate", "30",
                "-i", str(h264_path),
                "-c", "copy",
                str(mp4_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0:
            h264_path.unlink()
            return mp4_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return h264_path  # ffmpeg not available or failed; .h264 is playable in VLC


# ------------------------------------------------------------------
# Free capture
# ------------------------------------------------------------------

def free_still(cam_mgr, apply_ff: bool = False) -> dict:
    arr = cam_mgr.capture_still()
    if apply_ff:
        arr = apply_flat_field(arr, load_flat())
    ts = _ts()
    filename = f"{ts}_still.jpg"
    path = _free_dir() / filename
    t0 = time.perf_counter()
    save_jpeg(arr, path)
    log.debug("[TIMING] free_still: save_jpeg=%.3fs", time.perf_counter() - t0)
    return {"path": str(path), "filename": filename}


def free_video(cam_mgr, duration_s: int, bitrate_bps: int = DEFAULT_BITRATE) -> dict:
    ts = _ts()
    h264_path = _free_dir() / f"{ts}_video.h264"
    cam_mgr.start_video_recording(h264_path, bitrate_bps)
    try:
        time.sleep(duration_s)
    finally:
        cam_mgr.stop_video_recording()
    final = wrap_h264(h264_path)
    return {"path": str(final), "filename": final.name, "duration_s": duration_s}


# ------------------------------------------------------------------
# Plate capture
# ------------------------------------------------------------------

def plate_motility(
    cam_mgr,
    plate_dir: Path,
    duration_s: int,
    bitrate_bps: int = DEFAULT_BITRATE,
) -> dict:
    ts = _ts()
    h264_path = plate_dir / f"{ts}_video.h264"
    cam_mgr.start_video_recording(h264_path, bitrate_bps)
    try:
        time.sleep(duration_s)
    finally:
        cam_mgr.stop_video_recording()
    final = wrap_h264(h264_path)
    return {
        "path": str(final),
        "filename": final.name,
        "duration_s": duration_s,
        "assay_mode": "motility",
    }


def plate_survival(
    cam_mgr,
    plate_dir: Path,
    quadrant: Optional[str] = None,
    apply_ff: bool = False,
) -> dict:
    arr = cam_mgr.capture_still()
    if apply_ff:
        arr = apply_flat_field(arr, load_flat())
    ts = _ts()
    filename = f"{ts}_{quadrant.upper()}.jpg" if quadrant else f"{ts}_still.jpg"
    path = plate_dir / filename
    t0 = time.perf_counter()
    save_jpeg(arr, path)
    log.debug("[TIMING] plate_survival: save_jpeg=%.3fs", time.perf_counter() - t0)
    return {
        "path": str(path),
        "filename": filename,
        "assay_mode": "survival",
        "quadrant": quadrant,
    }
