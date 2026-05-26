"""
Path-traces video render for the crawling pipeline.

Self-contained — does not touch the shared render_video.py. Reads centroid
positions from the trajectories_data table in <stem>_skeletons.hdf5
(worm_index_joined, frame_number, coord_x, coord_y) and overlays a fading
motion trail on a darkened copy of the source video.

Frames are read lazily (one at a time) and piped to ffmpeg for H.264 encoding,
matching the memory profile and encoding settings of render_video.py.
"""
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# /trajectories_data in _skeletons.hdf5 uses worm_index_joined (== timeseries
# worm_index). coord_x / coord_y are the centroid positions.
_WORM_INDEX_COL = "worm_index_joined"

# Darken factor applied to the source frame so traces stand out.
_DARKEN = 0.5

# 20-colour categorical BGR palette (tab20-style), cycled on worm_index mod 20.
_PALETTE: list[tuple[int, int, int]] = [
    (180,  31,  31), ( 14, 127, 255), ( 44, 160,  44), ( 40,  39, 214),
    (189, 103, 148), ( 75,  86, 140), (194, 119, 227), (127, 127, 127),
    ( 34, 189, 188), (207, 190,  23), (219, 119,  31), (120, 187, 255),
    (150, 152, 152), (213, 176, 197), (137, 232, 152), (130, 120, 255),
    (170, 220, 219), (211, 218, 158), (200, 165, 196), (148, 156, 196),
]


def _palette_color(worm_index: int) -> tuple[int, int, int]:
    return _PALETTE[int(worm_index) % len(_PALETTE)]


@contextmanager
def _ffmpeg_writer(
    out_path: Path, fps: float, width: int, height: int
) -> Iterator[object]:
    """Pipe raw BGR24 frames into ffmpeg for H.264 encoding."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-pix_fmt", "bgr24",
        "-i", "pipe:0",
        "-vcodec", "libx264", "-preset", "fast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    try:
        yield proc.stdin
    finally:
        if proc.stdin:
            proc.stdin.close()
        ret = proc.wait()
        if ret != 0:
            log.warning("ffmpeg exited %d writing %s", ret, out_path.name)


def _blend_line(
    img: np.ndarray,
    p1: tuple[int, int],
    p2: tuple[int, int],
    color: tuple[int, int, int],
    alpha: float,
    thickness: int,
) -> None:
    """
    Alpha-blend a single line segment onto img in-place, compositing only over
    the segment's bounding ROI so per-segment age fades stay cheap.
    """
    import cv2

    if alpha <= 0.0:
        return
    if alpha >= 1.0:
        cv2.line(img, p1, p2, color, thickness, cv2.LINE_AA)
        return

    x1, y1 = p1
    x2, y2 = p2
    pad = thickness + 1
    h, w = img.shape[:2]
    xmin = max(0, min(x1, x2) - pad)
    xmax = min(w, max(x1, x2) + pad + 1)
    ymin = max(0, min(y1, y2) - pad)
    ymax = min(h, max(y1, y2) + pad + 1)
    if xmax <= xmin or ymax <= ymin:
        return

    roi = img[ymin:ymax, xmin:xmax]
    overlay = roi.copy()
    cv2.line(overlay, (x1 - xmin, y1 - ymin), (x2 - xmin, y2 - ymin),
             color, thickness, cv2.LINE_AA)
    cv2.addWeighted(overlay, alpha, roi, 1.0 - alpha, 0.0, dst=roi)


def render_path_traces(
    avi_path: Path,
    skeletons_hdf5: Path,
    out_path: Path,
    fps: float,
    window_s: float = 10.0,
    kept_ids: "set[int] | None" = None,
) -> None:
    """
    Render an MP4 (same fps + resolution as source) showing each worm's recent
    centroid trail. For frame N, segments from max(0, N - window) to N are drawn
    per worm with a linear age-based alpha fade, and the current centroid is
    marked with a small filled circle. window = window_s * fps frames.

    kept_ids, when given, restricts the trails to those worm_index_joined values
    (crawling quality filter); None draws every track.
    """
    import cv2
    import pandas as pd

    log.info("Starting render: %s", out_path)

    try:
        traj = pd.read_hdf(str(skeletons_hdf5), key="trajectories_data")
    except Exception:
        log.warning("render_path_traces: cannot read trajectories_data from %s",
                    skeletons_hdf5, exc_info=True)
        return

    required = {_WORM_INDEX_COL, "frame_number", "coord_x", "coord_y"}
    if not required <= set(traj.columns):
        log.warning(
            "render_path_traces: required columns %s missing in %s (have %s) — skipping",
            sorted(required), skeletons_hdf5.name, list(traj.columns),
        )
        return

    df = pd.DataFrame({
        "wi": traj[_WORM_INDEX_COL].values,
        "fn": traj["frame_number"].values,
        "x": traj["coord_x"].values,
        "y": traj["coord_y"].values,
    }).dropna(subset=["wi", "fn", "x", "y"])

    if df.empty:
        log.warning("render_path_traces: no valid centroid rows in %s — skipping",
                    skeletons_hdf5.name)
        return

    # Pre-index per-worm sorted (frames, x, y) arrays for windowed slicing.
    # kept_ids (when given) restricts the trails to filter-passing worms.
    worm_tracks: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for w, g in df.groupby("wi"):
        if kept_ids is not None and int(w) not in kept_ids:
            continue
        g = g.sort_values("fn")
        worm_tracks[int(w)] = (
            g["fn"].values.astype(np.int64),
            g["x"].values.astype(np.float64),
            g["y"].values.astype(np.float64),
        )
    worm_color = {w: _palette_color(w) for w in worm_tracks}

    window = max(1, int(round(window_s * fps)))

    cap = cv2.VideoCapture(str(avi_path))
    if not cap.isOpened():
        log.warning("render_path_traces: cannot open %s", avi_path.name)
        return

    w_px = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h_px = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    try:
        with _ffmpeg_writer(out_path, fps, w_px, h_px) as pipe:
            frame_num = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                # Darkened base for contrast.
                base = (frame.astype(np.float32) * _DARKEN).astype(np.uint8)
                start_f = max(0, frame_num - window)

                for w, (wf, wx, wy) in worm_tracks.items():
                    lo = int(np.searchsorted(wf, start_f, side="left"))
                    hi = int(np.searchsorted(wf, frame_num, side="right"))
                    if hi - lo < 1:
                        continue
                    seg_f = wf[lo:hi]
                    seg_x = wx[lo:hi]
                    seg_y = wy[lo:hi]
                    color = worm_color[w]

                    for k in range(1, len(seg_f)):
                        age = frame_num - int(seg_f[k])      # 0 = newest
                        alpha = max(0.0, 1.0 - age / window)  # linear age fade
                        _blend_line(
                            base,
                            (int(seg_x[k - 1]), int(seg_y[k - 1])),
                            (int(seg_x[k]), int(seg_y[k])),
                            color, alpha, thickness=2,
                        )

                    # Current centroid marker (most recent point in the window).
                    cv2.circle(base, (int(seg_x[-1]), int(seg_y[-1])),
                               4, color, -1, cv2.LINE_AA)

                pipe.write(base.tobytes())
                frame_num += 1
        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_path_traces failed writing %s", out_path, exc_info=True)
    finally:
        cap.release()
