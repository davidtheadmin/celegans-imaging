"""
Illumination-gradient correction, applied during the mp4 -> AVI transcode.

Why this exists
---------------
The capture head does not illuminate the field evenly. On 20260530T153913
(N2 10J, day1) the temporal-median background runs 106 counts at the frame
centre to 169 in the corners, +60%, and Tierpsy's skeleton yield tracks that
gradient exactly:

    r <200 px   99.3%        r 600-800    81.7%
    r 200-400   86.9%        r 800-1000   51.4%
    r 400-600   90.1%        r >1000      31.2%

63% of all lost frames sat within 200 px of a frame border. The mechanism is
the brightness RAMP inside one worm's ROI — 1.6 counts across a 110 px ROI at
the centre, 8.7 at r 800-1000. Tierpsy's per-ROI threshold sits about 7 counts
above local background everywhere (113 vs 105 at the centre, 161 vs 154 at the
rim), so at the rim it admits the bright half of the ramp, the blob inflates
(1392 -> 2139 px), there are no clean head/tail curvature peaks, contour
splitting fails, and has_skeleton goes to 0 with contour_area NaN beside it.

No Tierpsy parameter fixes this. The per-ROI threshold is ALREADY locally
adaptive — that is exactly why it tracks the background from 113 to 161 — and
worm_bw_thresh_factor is a single global scalar against a spatial defect.

Subtract, do not divide
-----------------------
Sampling real worms against their local background:

    radius        background   worm-bg   worm/bg
    0-200            105          50      1.48
    400-600          116          57      1.49
    800-1000         153          59      1.40
    1000-1300        160        58.5      1.37

Background climbs 1.45x while ABSOLUTE worm contrast barely moves (50 -> 59).
That is additive stray light, not a multiplicative gain, so the correction is
a subtraction. Measured on corrected video, worm contrast by radius:

    input       r<400            400-800          r>800          rim/centre
    RAW         bg 107, c 53     bg 120, c 58     bg 162, c 55      1.05
    SUBTRACT    bg 133, c 52     bg 133, c 57     bg 134, c 53      1.02
    DIVIDE      bg 132, c 64     bg 133, c 64     bg 134, c 43      0.67

Division flattens the background just as well but strips a third of the
contrast off the rim worms, making the periphery worse than uncorrected.

Result on the test video (arm7 = this correction + thresh_block_size 61,
thresh_C 10): skeleton yield 0.692 -> 0.909, corner yield 0.312 -> 0.835,
skeletons surviving SKE_FILT 0.573 -> 0.868, tracked worm-seconds essentially
unchanged (2075.7 -> 2090.9) because detection was never the problem.

The field
---------
Temporal median of SAMPLE_FRAMES frames spread over the video, then heavily
Gaussian-smoothed. Smoothed rather than raw because illumination is
low-frequency while worms and lawn are not: a smoothed field cannot bake in a
worm that never moved, and it leaves plate-to-plate lawn texture alone. The
raw-median variant was tested (arm3_subx) and gave no benefit — same yield,
worse fragmentation, 162 fragments against 131 — while carrying that risk.

The field is cached beside the AVI as <stem>_flatfield.npy with a PNG preview,
so a run is reproducible and you can look at what was subtracted.

Videos that do not need it are left alone: if rim/centre is below
MIN_GRADIENT the correction is skipped and the plain ffmpeg transcode runs.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SAMPLE_FRAMES: int = 120      # frames sampled to build the temporal median
BLUR_SIGMA: float = 80.0      # px; illumination is low-frequency, worms are not
MIN_GRADIENT: float = 1.10    # rim/centre below this -> leave the video alone
_CENTRE_R: float = 200.0      # px, radius of the "centre" disc
_RIM_FRAC: float = 0.65       # rim = r > _RIM_FRAC * min(h, w)


def _shape_of(video: Path) -> "tuple[int, int] | None":
    """(h, w) of the video's frames, without decoding one."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            return None
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return (h, w) if h > 0 and w > 0 else None
    finally:
        cap.release()


def field_path(avi: Path) -> Path:
    """Where the cached illumination field for this AVI lives."""
    return avi.with_name(avi.stem + "_flatfield.npy")


def measure_field(video: Path) -> "tuple[np.ndarray, float]":
    """
    Build the smoothed illumination field for `video`.

    Returns (field, rim_over_centre). Raises RuntimeError if no frame could be
    read. The field is float32, same shape as one greyscale frame.
    """
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video}")
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        idx = np.linspace(0, max(n - 1, 0), min(SAMPLE_FRAMES, max(n, 1))).astype(int)
        frames = []
        for i in idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, f = cap.read()
            if ok:
                frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
    finally:
        cap.release()
    if not frames:
        raise RuntimeError(f"could not sample any frame from {video}")

    med = np.median(np.stack(frames), axis=0).astype(np.float32)
    h, w = med.shape

    # Smooth at 1/8 scale — same result as a full-size sigma-80 blur, far cheaper.
    small = cv2.resize(med, (max(w // 8, 1), max(h // 8, 1)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), BLUR_SIGMA / 8.0)
    field = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)

    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(xx - w / 2.0, yy - h / 2.0)
    centre = float(field[r < _CENTRE_R].mean())
    rim = float(field[r > _RIM_FRAC * min(h, w)].mean())
    ratio = (rim / centre) if centre > 0 else 1.0
    return field, ratio


def load_or_build_field(video: Path, cache: Path,
                        write_preview: bool = True) -> "tuple[np.ndarray, float]":
    """Cached measure_field. The .npy holds the field; the ratio is recomputed."""
    import cv2

    if cache.exists():
        try:
            # Stored at 1/8 scale: the field is smoothed at sigma 80, so a
            # full-resolution copy is ~12 MB per video of pure redundancy.
            small = np.load(cache).astype(np.float32)
            shape = _shape_of(video)
            if shape is None:
                raise RuntimeError("cannot size the video")
            h, w = shape
            field = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
            yy, xx = np.mgrid[0:h, 0:w]
            r = np.hypot(xx - w / 2.0, yy - h / 2.0)
            centre = float(field[r < _CENTRE_R].mean())
            rim = float(field[r > _RIM_FRAC * min(h, w)].mean())
            return field, ((rim / centre) if centre > 0 else 1.0)
        except Exception:
            log.warning("flat-field cache %s unreadable, rebuilding", cache.name,
                        exc_info=True)

    field, ratio = measure_field(video)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        h, w = field.shape
        np.save(cache, cv2.resize(field, (max(w // 8, 1), max(h // 8, 1)),
                                  interpolation=cv2.INTER_AREA))
        if write_preview:
            vis = cv2.applyColorMap(
                cv2.normalize(field, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8),
                cv2.COLORMAP_VIRIDIS)
            cv2.imwrite(str(cache.with_suffix(".png")),
                        cv2.resize(vis, (1200, 900)))
    except Exception:
        log.warning("could not cache flat field to %s", cache, exc_info=True)
    return field, ratio
def _drain(pipe, sink: list) -> None:
    """Read a pipe to EOF into `sink`. Runs on its own thread.

    This exists because of a real failure: transcode_corrected used to take
    ffmpeg's stderr on a PIPE and only read it after proc.wait(). ffmpeg writes
    progress continuously, the few-KB OS pipe buffer filled, ffmpeg blocked on
    write and could never exit, and wait() blocked forever waiting for the exit.
    Two processes idle against each other, no CPU, no progress, no error — it
    cost a 45-minute run before anyone noticed. Never put a subprocess pipe
    behind a wait() again: drain it concurrently, or don't create it.
    """
    try:
        for chunk in iter(lambda: pipe.read(65536), b""):
            sink.append(chunk)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _run_ffmpeg(cmd: list, feed=None, timeout_s: int = 3600) -> None:
    """Run ffmpeg with its stderr drained concurrently. Raises on failure.

    `feed(stdin)` , when given, writes the input stream; stdin is closed and
    the process reaped afterwards. Never deadlocks: stderr has its own reader
    thread for the whole lifetime of the process.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=(subprocess.PIPE if feed is not None else subprocess.DEVNULL),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    sink: list = []
    reader = threading.Thread(target=_drain, args=(proc.stderr, sink),
                              daemon=True)
    reader.start()
    try:
        if feed is not None:
            try:
                feed(proc.stdin)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        reader.join(timeout=5)
        raise RuntimeError(f"ffmpeg timed out after {timeout_s}s")
    finally:
        reader.join(timeout=10)
    if rc != 0:
        err = b"".join(sink).decode("utf-8", "replace").strip()[-800:]
        raise RuntimeError(f"ffmpeg exited {rc}: {err}")


def verify_avi(path: Path) -> None:
    """Raise unless `path` is a finalised AVI.

    ffmpeg writes 0xFFFFFFFF as the RIFF size while muxing and patches in the
    real value, then appends the idx1 index, only on a clean exit. A file with
    the placeholder still in place is a half-written video that OpenCV and
    Tierpsy will happily open and silently read short. Checking is two seeks,
    and the alternative is a whole analysis run built on truncated input.
    """
    size = path.stat().st_size
    if size < 1024:
        raise RuntimeError(f"{path.name} is only {size} bytes")
    with path.open("rb") as f:
        head = f.read(12)
        if head[0:4] != b"RIFF" or head[8:12] != b"AVI ":
            raise RuntimeError(f"{path.name} is not an AVI")
        declared = int.from_bytes(head[4:8], "little")
        f.seek(max(0, size - 4_000_000))
        tail = f.read()
    if declared == 0xFFFFFFFF or declared + 8 != size:
        raise RuntimeError(
            f"{path.name} was never finalised (RIFF size {declared} vs file "
            f"{size}) — ffmpeg did not exit cleanly")
    if b"idx1" not in tail:
        raise RuntimeError(f"{path.name} has no idx1 index — truncated")


def _write_operand(field: np.ndarray, dst: Path) -> None:
    """The image ffmpeg subtracts: (field - mean) * 256 + 32768, 16-bit grey.

    blend=all_mode=grainextract computes `first - second + midpoint`, so with
    the video promoted to gray16 this yields `frame - field + mean(field)` —
    the same arithmetic the numpy path does.

    SIXTEEN bit, not eight, and that is not fussiness. At 8 bit the operand is
    rounded to whole counts before the subtraction and the whole computation is
    integer, which measured 1.743 counts of mean error against the numpy
    reference. At 16 bit it is 0.916, against the numpy path's own 0.838 —
    i.e. indistinguishable, the residual in both being MJPEG recompression.
    The field is smooth, so 8-bit error is a systematic pattern rather than
    noise, and it lands on exactly the low-contrast rim worms this correction
    exists to rescue.
    """
    import cv2

    biased = (field - float(field.mean())) * 256.0 + 32768.0
    lo, hi = float(biased.min()), float(biased.max())
    if lo < 0 or hi > 65535:
        raise RuntimeError(
            f"illumination field spans {lo:.0f}..{hi:.0f} after biasing, "
            "outside 16-bit range")
    cv2.imwrite(str(dst), np.clip(biased, 0, 65535).astype(np.uint16))


def transcode_corrected(src: Path, avi: Path, field: np.ndarray,
                        threads: "int | None" = None) -> None:
    """
    Decode `src`, subtract `field`, encode MJPEG q3 to `avi` — ONE pass, so the
    correction costs no extra generation of lossy compression over the plain
    transcode it replaces.

    Output is `frame - field + mean(field)`, which flattens the background while
    leaving absolute worm contrast untouched.

    Done inside ffmpeg's own filter graph (blend=grainextract against a
    pre-biased operand image), so the pixels never enter Python. The previous
    implementation pushed every frame through a numpy loop and cost about two
    minutes per video; across a full reanalysis that is most of a day of pure
    transcoding. `_transcode_python` is kept as a fallback for anything the
    filter path cannot handle.
    """
    shape = _shape_of(src)
    if shape is None:
        raise RuntimeError(f"cannot read frame size from {src}")
    h, w = shape
    if field.shape != (h, w):
        raise RuntimeError(
            f"flat field {field.shape} does not match video {(h, w)}")

    operand = avi.with_name(avi.stem + "_ffop.png")
    try:
        _write_operand(field, operand)
        cmd = [
            "ffmpeg", "-y", "-nostats", "-loglevel", "error",
            "-i", str(src),
            "-loop", "1", "-i", str(operand),
            "-filter_complex",
            # shortest=1 is load-bearing: the operand is a LOOPED still, so
            # without it framesync keeps the graph alive after the video
            # ends and ffmpeg encodes forever. The output-level -shortest
            # does not govern a filter-graph output.
            "[0:v]format=gray16le[v];[v][1:v]"
            "blend=all_mode=grainextract:shortest=1,format=gray[o]",
            "-map", "[o]", "-an", "-shortest",
            "-vcodec", "mjpeg", "-q:v", "3",
        ]
        if threads is not None:
            cmd += ["-threads", str(threads)]
        cmd.append(str(avi))
        _run_ffmpeg(cmd)
        verify_avi(avi)
        log.info("flat-field corrected %s -> %s (ffmpeg filter)",
                 src.name, avi.name)
    except Exception:
        log.warning("flat-field: the ffmpeg filter path failed for %s; "
                    "falling back to the frame loop", src.name, exc_info=True)
        if avi.exists():
            try:
                avi.unlink()
            except OSError:
                pass
        _transcode_python(src, avi, field, threads=threads)
    finally:
        if operand.exists():
            try:
                operand.unlink()
            except OSError:
                pass


def _transcode_python(src: Path, avi: Path, field: np.ndarray,
                      threads: "int | None" = None) -> None:
    """Fallback: the same correction, frame by frame through numpy."""
    import cv2

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    offset = (float(field.mean()) - field).astype(np.float32)
    n = {"count": 0}

    cmd = [
        "ffmpeg", "-y", "-nostats", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{w}x{h}", "-r", str(fps), "-pix_fmt", "gray",
        "-i", "pipe:0",
        "-vcodec", "mjpeg", "-q:v", "3",
    ]
    if threads is not None:
        cmd += ["-threads", str(threads)]
    cmd.append(str(avi))

    def feed(stdin) -> None:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            stdin.write(np.clip(g + offset, 0, 255).astype(np.uint8).tobytes())
            n["count"] += 1

    try:
        _run_ffmpeg(cmd, feed=feed)
    finally:
        cap.release()
    if n["count"] == 0:
        raise RuntimeError(f"no frames decoded from {src}")
    verify_avi(avi)
    log.info("flat-field corrected %s -> %s (%d frames, numpy fallback)",
             src.name, avi.name, n["count"])
