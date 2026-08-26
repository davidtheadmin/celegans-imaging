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


# Worm-number label sizing (matches the motility tracked-render style).
_ID_FONT_SCALE: float = 1.0
_ID_FONT_THICKNESS: int = 2

# Reversal flash: a bright expanding ring drawn on a worm's trail for frames
# within ±_FLASH_HALF_S of a frame where that worm reversed.
_FLASH_HALF_S: float = 0.25
_FLASH_COLOR: tuple[int, int, int] = (0, 255, 255)  # yellow (BGR)


def _draw_label(img: "np.ndarray", text: str, pos: tuple[int, int],
                color: tuple[int, int, int]) -> None:
    """Draw a worm-index label with a thin black outline for legibility."""
    import cv2

    anchor = (pos[0] + 6, pos[1] - 6)
    cv2.putText(img, text, anchor, cv2.FONT_HERSHEY_SIMPLEX, _ID_FONT_SCALE,
                (0, 0, 0), _ID_FONT_THICKNESS + 2, cv2.LINE_AA)
    cv2.putText(img, text, anchor, cv2.FONT_HERSHEY_SIMPLEX, _ID_FONT_SCALE,
                color, _ID_FONT_THICKNESS, cv2.LINE_AA)


def render_path_traces(
    avi_path: Path,
    skeletons_hdf5: Path,
    out_path: Path,
    fps: float,
    window_s: float = 10.0,
    worm_index_map: "dict | None" = None,
    reversal_frames: "dict[int, list] | None" = None,
    arrow_data: "dict[int, dict] | None" = None,
) -> None:
    """
    Render an MP4 (same fps + resolution as source) showing each worm's recent
    centroid trail. For frame N, segments from max(0, N - window) to N are drawn
    per worm with a linear age-based alpha fade, the current centroid is marked
    with a small filled circle, and a stable worm-index number labels the worm.
    window = window_s * fps frames.

    worm_index_map maps each member worm_index_joined to its stable grouped
    worm_index (crawling quality filter): trails are grouped and coloured by that
    worm_index, fragments absent from the map are filtered out and not drawn.
    When None, every track is drawn keyed by its own worm_index_joined.

    reversal_frames maps a grouped worm_index to the list of frame numbers where
    that worm reversed; at those frames (±_FLASH_HALF_S) a bright ring pulses on
    the worm's current centroid.

    arrow_data (optional) draws a per-frame velocity arrow per kept worm — the
    SAME overlay as the tracked render, but arrows ONLY (no reversal/turn event
    markers: the path-traces video is already dense with trail content, so flashing
    markers would clutter; the clean directional arrow stays).
    """
    import cv2
    import pandas as pd

    from analysis.render_video import _prepare_arrow_worms, _draw_arrows_and_markers

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

    # Remap member worm_index_joined → grouped worm_index, accumulating each
    # grouped worm's member tracks. worm_index_map (when given) restricts the
    # trails to filter-passing worms.
    accum: dict[int, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
    for w, g in df.groupby("wi"):
        g = g.sort_values("fn")
        fn = g["fn"].values.astype(np.int64)
        xs = g["x"].values.astype(np.float64)
        ys = g["y"].values.astype(np.float64)
        if worm_index_map is None:
            accum.setdefault(int(w), []).append((fn, xs, ys))
            continue
        # A fragment cut at a collision belongs to different tracks in
        # different frame windows, so its trail is split the same way — see
        # render_video.resolve_worm_id.
        v = worm_index_map.get(int(w))
        if v is None:
            continue
        spans = ([(-1, 1 << 62, int(v))] if isinstance(v, (int, np.integer))
                 else [(int(a), int(b), int(gi)) for a, b, gi in v])
        for f0, f1, gi in spans:
            m = (fn >= f0) & (fn <= f1)
            if m.any():
                accum.setdefault(gi, []).append((fn[m], xs[m], ys[m]))

    # Merge each grouped worm's member tracks into one frame-sorted track.
    worm_tracks: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for gi, parts in accum.items():
        wf = np.concatenate([p[0] for p in parts])
        wx = np.concatenate([p[1] for p in parts])
        wy = np.concatenate([p[2] for p in parts])
        order = np.argsort(wf, kind="stable")
        worm_tracks[gi] = (wf[order], wx[order], wy[order])
    worm_color = {gi: _palette_color(gi) for gi in worm_tracks}

    # Per-worm sorted reversal-frame arrays for fast nearest-frame lookup.
    rev_by_worm: dict[int, np.ndarray] = {}
    if reversal_frames:
        for gi, frames in reversal_frames.items():
            if frames:
                rev_by_worm[int(gi)] = np.sort(np.asarray(frames, dtype=np.int64))

    arrow_worms = _prepare_arrow_worms(arrow_data)

    window = max(1, int(round(window_s * fps)))
    flash_half = max(2, int(round(_FLASH_HALF_S * fps)))

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

                    cur = (int(seg_x[-1]), int(seg_y[-1]))

                    # Reversal flash: bright ring when near one of this worm's
                    # reversal frames; radius grows toward the exact frame.
                    rev = rev_by_worm.get(w)
                    if rev is not None and len(rev):
                        j = int(np.searchsorted(rev, frame_num))
                        d = window  # large default
                        if j < len(rev):
                            d = min(d, int(rev[j] - frame_num))
                        if j > 0:
                            d = min(d, int(frame_num - rev[j - 1]))
                        if d <= flash_half:
                            intensity = 1.0 - d / flash_half
                            radius = int(8 + 16 * intensity)
                            cv2.circle(base, cur, radius, _FLASH_COLOR, 2, cv2.LINE_AA)

                    # Current centroid marker + stable worm-index label.
                    cv2.circle(base, cur, 4, color, -1, cv2.LINE_AA)
                    _draw_label(base, str(w), cur, color)

                # Velocity arrows only (no event markers — see docstring).
                if arrow_worms:
                    _draw_arrows_and_markers(base, arrow_worms, frame_num,
                                             marker_duration=0, draw_markers=False)

                pipe.write(base.tobytes())
                frame_num += 1
        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_path_traces failed writing %s", out_path, exc_info=True)
    finally:
        cap.release()
