import hashlib
import io
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import HTTPException
from PIL import Image

from .camera import FULL_W
from .config import settings

log = logging.getLogger(__name__)

# capture.py lives at capture/capture.py (sibling of this package's parent dir).
# WorkingDirectory for the service is capture/, so parents[1] = capture/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture import apply_flat_field  # noqa: E402
from capture import load_master_flat as _load_master_flat  # noqa: E402

DEFAULT_BITRATE = 9_000_000  # 9 Mbps for 2028×1520 @ 30 fps
DEFAULT_DURATION = 30
DEFAULT_VIDEO_FPS = 30  # fallback; actual fps queried from camera before recording


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

def _flat_dir() -> Path:
    return Path(settings.DATA_ROOT) / "flatfield"


def _free_dir() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    d = Path(settings.DATA_ROOT) / settings.PICTURES_DIR / today
    d.mkdir(parents=True, exist_ok=True)
    return d


def _video_dir() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    d = Path(settings.DATA_ROOT) / settings.VIDEOS_DIR / today
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _write_sha256(path: Path) -> str:
    """Compute SHA256 of path and write hex digest to <path>.sha256. Returns the digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    sha_path = path.parent / (path.name + ".sha256")
    tmp = sha_path.with_suffix(".tmp")
    tmp.write_text(digest)
    tmp.replace(sha_path)
    log.debug("_write_sha256: %s -> %s", path.name, digest[:12])
    return digest


def read_sha256(path: Path) -> Optional[str]:
    """Read cached SHA256 for path, or compute+cache it lazily if missing."""
    sha_path = path.parent / (path.name + ".sha256")
    if sha_path.exists():
        return sha_path.read_text().strip()
    if not path.exists():
        return None
    return _write_sha256(path)


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

def save_still(arr: np.ndarray, path: Path, um_per_px: Optional[float] = None) -> None:
    """Save an RGB array as an LZW-compressed TIFF. When um_per_px is provided,
    embed ImageJ-readable spatial calibration (resolution tags + unit in the
    ImageDescription) so the file opens pre-scaled in microns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(arr, "RGB")
    if um_per_px is not None and um_per_px > 0:
        px_per_um = 1.0 / um_per_px
        tiffinfo = {
            282: px_per_um,                 # XResolution (pixels per unit)
            283: px_per_um,                 # YResolution
            296: 1,                         # ResolutionUnit = none; unit given in description
            270: "ImageJ=1.54f\nunit=um\n",  # ImageDescription
        }
        img.save(path, format="TIFF", compression="tiff_lzw", tiffinfo=tiffinfo)
    else:
        img.save(path, format="TIFF", compression="tiff_lzw")


# ------------------------------------------------------------------
# ffmpeg wrapping
# ------------------------------------------------------------------

def wrap_h264(h264_path: Path, fps: float = DEFAULT_VIDEO_FPS) -> Path:
    """Remux .h264 -> .mp4 with correct framerate. Falls back to raw .h264.
    -r before -i is the correct flag for the H264 demuxer (generates PTS from
    frame count); -framerate is a V4L2/device option and is silently ignored
    for file inputs."""
    mp4_path = h264_path.with_suffix(".mp4")
    log.debug("wrap_h264: remuxing %s at %.3f fps", h264_path.name, fps)
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-r", f"{fps:.6f}",
                "-i", str(h264_path),
                "-c", "copy",
                str(mp4_path),
            ],
            capture_output=True,
            timeout=120,
        )
        elapsed = time.perf_counter() - t0
        if result.returncode == 0:
            log.debug("wrap_h264: done in %.3fs -> %s", elapsed, mp4_path.name)
            h264_path.unlink()
            return mp4_path
        log.warning("wrap_h264: ffmpeg rc=%d in %.3fs stderr=%s",
                    result.returncode, elapsed, result.stderr[-200:].decode(errors="replace"))
    except FileNotFoundError:
        log.warning("wrap_h264: ffmpeg not found, keeping .h264")
    except subprocess.TimeoutExpired:
        log.warning("wrap_h264: ffmpeg timed out after %.3fs", time.perf_counter() - t0)
    return h264_path  # ffmpeg not available or failed; .h264 is playable in VLC


# ------------------------------------------------------------------
# Free capture
# ------------------------------------------------------------------

def free_still(cam_mgr, apply_ff: bool = False) -> dict:
    arr = cam_mgr.capture_still()
    if apply_ff:
        arr = apply_flat_field(arr, load_flat())
    ts = _ts()
    filename = f"{ts}_still.tif"
    path = _free_dir() / filename
    um_per_px = cam_mgr.active_um_per_px(FULL_W)  # full-frame scale; cropping-invariant
    t0 = time.perf_counter()
    save_still(arr, path, um_per_px)
    log.debug("[TIMING] free_still: save_still=%.3fs", time.perf_counter() - t0)
    _write_sha256(path)
    return {"path": str(path), "filename": filename}


def free_video(cam_mgr, duration_s: int, bitrate_bps: int = DEFAULT_BITRATE) -> dict:
    ts = _ts()
    h264_path = _video_dir() / f"{ts}_video.h264"
    log.debug("free_video: starting recording -> %s", h264_path.name)
    cam_mgr.start_video_recording(h264_path, bitrate_bps)
    fps = cam_mgr.video_fps  # read after reconfiguration so we get the actual video fps
    log.debug("free_video: using %.2f fps", fps)
    try:
        time.sleep(duration_s)
    finally:
        log.debug("free_video: stopping recording")
        cam_mgr.stop_video_recording()
        log.debug("free_video: recording stopped (including drain pause)")
    final = wrap_h264(h264_path, fps=fps)
    _write_sha256(final)
    log.debug("free_video: returning %s", final.name)
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
    log.debug("plate_motility: starting recording -> %s", h264_path.name)
    cam_mgr.start_video_recording(h264_path, bitrate_bps)
    fps = cam_mgr.video_fps  # read after reconfiguration so we get the actual video fps
    log.debug("plate_motility: using %.2f fps", fps)
    try:
        time.sleep(duration_s)
    finally:
        log.debug("plate_motility: stopping recording")
        cam_mgr.stop_video_recording()
        log.debug("plate_motility: recording stopped (including drain pause)")
    final = wrap_h264(h264_path, fps=fps)
    _write_sha256(final)
    log.debug("plate_motility: returning %s", final.name)
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
    filename = f"{ts}_{quadrant.upper()}.tif" if quadrant else f"{ts}_still.tif"
    path = plate_dir / filename
    um_per_px = cam_mgr.active_um_per_px(FULL_W)  # full-frame scale; cropping-invariant
    t0 = time.perf_counter()
    save_still(arr, path, um_per_px)
    log.debug("[TIMING] plate_survival: save_still=%.3fs", time.perf_counter() - t0)
    _write_sha256(path)
    return {
        "path": str(path),
        "filename": filename,
        "assay_mode": "survival",
        "quadrant": quadrant,
    }


# ------------------------------------------------------------------
# File serving helpers
# ------------------------------------------------------------------

THUMB_LONG = 400
_VIDEO_EXTS = {".mp4", ".h264", ".mkv"}
_THUMB_EXTS = {".jpg", ".jpeg", ".tif", ".tiff"} | _VIDEO_EXTS
_SIDECAR_SUFFIXES = {".sha256", ".acked"}  # never shown in file listings


def free_base() -> Path:
    return Path(settings.DATA_ROOT) / settings.PICTURES_DIR


def video_base() -> Path:
    return Path(settings.DATA_ROOT) / settings.VIDEOS_DIR


def trash_base() -> Path:
    return Path(settings.DATA_ROOT) / ".trash"


def trash_file(src: Path, rel_path: str) -> Path:
    """Move src (and its .thumbs cache) to .trash/<rel_path>. Returns dest path."""
    dest = trash_base() / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")
    shutil.move(str(src), str(dest))
    thumb_src = src.parent / ".thumbs" / (src.stem + ".jpg")
    if thumb_src.exists():
        thumb_dest = dest.parent / ".thumbs" / (dest.stem + ".jpg")
        thumb_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(thumb_src), str(thumb_dest))
        except OSError:
            pass
    return dest


def make_thumb(image_path: Path) -> bytes:
    """Return cached thumbnail JPEG bytes (400px on longest side).
    For videos, extracts the first frame via ffmpeg."""
    cache = image_path.parent / ".thumbs" / (image_path.stem + ".jpg")
    if cache.exists():
        return cache.read_bytes()
    if image_path.suffix.lower() in _VIDEO_EXTS:
        return _make_video_thumb(image_path, cache)
    img = Image.open(image_path)
    img.thumbnail((THUMB_LONG, THUMB_LONG), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    data = buf.getvalue()
    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(data)
    except OSError:
        pass
    return data


def _make_video_thumb(video_path: Path, cache: Path) -> bytes:
    # Create .thumbs/ directory BEFORE ffmpeg tries to write the tmp file into it.
    # Previously mkdir was called after subprocess.run, so ffmpeg always failed on
    # first use of a new .thumbs/ directory (no such directory error).
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp.jpg")
    log.debug("_make_video_thumb: extracting frame from %s", video_path.name)
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "0", "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale={THUMB_LONG}:{THUMB_LONG}:force_original_aspect_ratio=decrease",
                str(tmp),
            ],
            capture_output=True, timeout=30,
        )
        elapsed = time.perf_counter() - t0
        if result.returncode == 0 and tmp.exists():
            log.debug("_make_video_thumb: done in %.3fs -> %s", elapsed, cache.name)
            data = tmp.read_bytes()
            tmp.rename(cache)
            return data
        log.warning("_make_video_thumb: ffmpeg rc=%d for %s in %.3fs stderr=%s",
                    result.returncode, video_path.name, elapsed,
                    result.stderr[-300:].decode(errors="replace"))
    except FileNotFoundError:
        log.warning("_make_video_thumb: ffmpeg not found")
    except subprocess.TimeoutExpired:
        log.warning("_make_video_thumb: ffmpeg timed out for %s", video_path.name)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    raise HTTPException(404, "Could not generate video thumbnail")
