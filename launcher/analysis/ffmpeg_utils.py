import logging
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_DEPTH = 3
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def find_videos(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Recursively find .mp4 files up to max_depth levels deep, skipping dirs
    whose name starts with '_' or '.'.

    '_' marks pipeline-generated dirs (_wormscan_cache/, _crawling_analysis_*/);
    '.' marks hidden caches that follow the Unix convention (.viewer_cache/,
    .trash/, .thumbs/, .git/). Descending into either would process preview clips
    / cached artifacts as if they were experiment videos.
    """
    results: list[Path] = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() == ".mp4":
                    results.append(child)
                elif child.is_dir() and not (
                    child.name.startswith("_") or child.name.startswith(".")
                ):
                    _recurse(child, depth + 1)
        except PermissionError:
            pass

    _recurse(folder, 1)
    return results


def probe_fps(video: Path) -> float:
    """Return fps from ffprobe. Raises RuntimeError on failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=nw=1:nk=1",
            str(video),
        ],
        capture_output=True, text=True, timeout=30,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    raw = result.stdout.strip()
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"Could not parse fps '{raw}': {exc}") from exc


def probe_duration(video: Path) -> float:
    """Return video duration in seconds from ffprobe, or 0.0 on failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(video),
        ],
        capture_output=True, text=True, timeout=30,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        log.warning("ffprobe duration failed for %s: %s", video.name, result.stderr.strip())
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def convert_to_avi(mp4: Path, avi: Path, threads: int | None = None,
                   flat_field: bool = False) -> None:
    """
    Convert mp4 to MJPEG AVI at quality 3. Skips if avi already exists.
    Raises RuntimeError on ffmpeg failure.

    `threads` caps ffmpeg's thread count (decode+encode). Used when several
    transcodes run concurrently to avoid oversubscribing the CPU. None (the
    default) leaves ffmpeg's automatic threading unchanged. MJPEG (-q:v 3) is
    intra-frame, so the thread count does not affect the encoded output.

    `flat_field` subtracts the video's own illumination field during this same
    pass — see analysis.flatfield for why (the capture head lights the frame
    unevenly, which was costing roughly two thirds of the skeletons at the
    frame edges). Doing it here rather than as a second pass matters: the
    correction then adds no extra generation of lossy compression over the
    plain transcode it replaces. A video whose gradient is below
    flatfield.MIN_GRADIENT is left alone and takes the plain path.
    """
    if avi.exists():
        log.info("AVI already exists, skipping conversion: %s", avi.name)
        return

    if flat_field:
        from analysis import flatfield
        try:
            field, ratio = flatfield.load_or_build_field(
                mp4, flatfield.field_path(avi))
            if ratio >= flatfield.MIN_GRADIENT:
                log.info("flat-field: %s rim/centre %.2f -> correcting",
                         mp4.name, ratio)
                flatfield.transcode_corrected(mp4, avi, field, threads=threads)
                return
            log.info("flat-field: %s rim/centre %.2f is below %.2f -> "
                     "leaving the video uncorrected",
                     mp4.name, ratio, flatfield.MIN_GRADIENT)
        except Exception:
            # A correction failure must not cost the whole run: fall through to
            # the plain transcode and say so loudly.
            log.warning("flat-field correction failed for %s; falling back to "
                        "an uncorrected transcode", mp4.name, exc_info=True)
            if avi.exists():
                try:
                    avi.unlink()
                except OSError:
                    pass
    cmd = ["ffmpeg", "-y", "-i", str(mp4), "-vcodec", "mjpeg", "-q:v", "3"]
    if threads is not None:
        cmd += ["-threads", str(threads)]
    cmd.append(str(avi))
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=300,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")
    # An AVI that exists is not an AVI that finished. A killed or timed-out
    # ffmpeg leaves the RIFF size at its 0xFFFFFFFF placeholder and no idx1
    # index, and both OpenCV and Tierpsy will open such a file and silently
    # read it short — an entire analysis built on a truncated video, with
    # nothing anywhere saying so. Two seeks to rule it out.
    from analysis import flatfield
    try:
        flatfield.verify_avi(avi)
    except Exception:
        try:
            avi.unlink()
        except OSError:
            pass
        raise
