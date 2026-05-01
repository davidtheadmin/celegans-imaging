"""
Optional tracked-video rendering for motility analysis.

All three render functions read frames lazily — one at a time — so memory
stays flat regardless of video length.  Each writes an H.264 MP4 via a
subprocess pipe to ffmpeg (already required by the pipeline).

Skeleton data lives in Results/<stem>_skeletons.hdf5.
Masked frames live in MaskedVideos/<stem>.hdf5 (/mask dataset).
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

# /trajectories_data in _skeletons.hdf5 uses worm_index_joined, not worm_index.
# (_featuresN.hdf5 timeseries_data uses worm_index — a different table entirely.)
_WORM_INDEX_COL = "worm_index_joined"


def _check_skel_col(skeletons_hdf5: Path, caller: str) -> bool:
    """Return True if _WORM_INDEX_COL is present in /trajectories_data; else log and return False."""
    try:
        import pandas as pd
        cols = pd.read_hdf(str(skeletons_hdf5), key="trajectories_data", stop=0).columns.tolist()
    except Exception as exc:
        log.warning("%s: could not read trajectories_data columns from %s: %s",
                    caller, skeletons_hdf5.name, exc)
        return False
    if _WORM_INDEX_COL not in cols:
        log.warning(
            "%s: expected column '%s' not found in trajectories_data. "
            "Actual columns: %s — skipping render.",
            caller, _WORM_INDEX_COL, cols,
        )
        return False
    return True

# 20-colour BGR palette for worm IDs — cycles on index mod 20
_PALETTE: list[tuple[int, int, int]] = [
    ( 64, 255,  64), (255,  64,  64), ( 64,  64, 255), ( 64, 255, 255),
    (255, 255,  64), (255,  64, 255), (  0, 165, 255), (255, 128,   0),
    (  0, 255, 128), (128,   0, 255), (128, 255,   0), (  0, 128, 255),
    (200, 200,   0), (200,   0, 200), (  0, 200, 200), (255, 140,  50),
    (140,  50, 255), ( 50, 255, 140), (200, 100,   0), (100,   0, 200),
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


def _load_skeleton_map(
    skeletons_hdf5: Path,
) -> tuple[dict[int, list[tuple[int, int]]], np.ndarray]:
    """
    Load trajectories_data + skeleton array from _skeletons.hdf5.

    Returns:
        frame_map  — {frame_number: [(worm_index, skel_row_index), …]}
        skel_array — shape (N, 49, 2), column 0 = x, column 1 = y
    """
    import h5py
    import pandas as pd

    traj = pd.read_hdf(str(skeletons_hdf5), key="trajectories_data")
    with h5py.File(skeletons_hdf5, "r") as f:
        skel: np.ndarray = f["skeleton"][:]

    frame_nums = traj["frame_number"].values
    worm_idxs = traj[_WORM_INDEX_COL].values
    frame_map: dict[int, list[tuple[int, int]]] = {}
    for row_idx, (fn, wi) in enumerate(zip(frame_nums, worm_idxs)):
        if pd.isna(fn) or pd.isna(wi):
            continue
        frame_map.setdefault(int(fn), []).append((int(wi), row_idx))

    return frame_map, skel


def _draw_skeleton(
    frame: np.ndarray,
    skel: np.ndarray,
    row_idx: int,
    color: tuple[int, int, int],
    thickness: int = 1,
    label: str = "",
) -> None:
    """Draw a 49-point polyline on frame in-place, with an optional ID label."""
    import cv2

    pts = skel[row_idx].astype(np.int32).reshape(-1, 1, 2)  # (49, 1, 2)
    cv2.polylines(frame, [pts], False, color, thickness, cv2.LINE_AA)
    if label:
        head = (int(skel[row_idx, 0, 0]), int(skel[row_idx, 0, 1]))
        cv2.putText(frame, label, head,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1, cv2.LINE_AA)


# ---------------------------------------------------------------------------
# Public render functions
# ---------------------------------------------------------------------------

def render_tracked(
    avi_path: Path,
    skeletons_hdf5: Path,
    out_path: Path,
    fps: float,
) -> None:
    """Render the AVI with skeleton polylines and worm-index labels."""
    import cv2

    log.info("Starting render: %s", out_path)

    if not _check_skel_col(skeletons_hdf5, "render_tracked"):
        return

    try:
        frame_map, skel = _load_skeleton_map(skeletons_hdf5)
    except Exception:
        log.warning("render_tracked: could not load skeleton data for %s", out_path, exc_info=True)
        return

    cap = cv2.VideoCapture(str(avi_path))
    if not cap.isOpened():
        log.warning("render_tracked: cannot open %s", avi_path.name)
        return

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    try:
        with _ffmpeg_writer(out_path, fps, w, h) as pipe:
            frame_num = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                for wi, skel_idx in frame_map.get(frame_num, []):
                    try:
                        _draw_skeleton(frame, skel, skel_idx,
                                       _palette_color(wi), label=str(wi))
                    except Exception:
                        log.debug("render_tracked: bad skeleton frame %d worm %d — skipping",
                                  frame_num, wi)
                pipe.write(frame.tobytes())
                frame_num += 1
        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_tracked failed writing %s", out_path, exc_info=True)
    finally:
        cap.release()


def render_curvature(
    avi_path: Path,
    skeletons_hdf5: Path,
    featuresN_hdf5: Path,
    out_path: Path,
    fps: float,
) -> None:
    """Render skeleton coloured by detrended curvature sign: red=positive, blue=negative."""
    import cv2
    import pandas as pd

    log.info("Starting render: %s", out_path)

    if not _check_skel_col(skeletons_hdf5, "render_curvature"):
        return

    try:
        frame_map, skel = _load_skeleton_map(skeletons_hdf5)
        ts = pd.read_hdf(str(featuresN_hdf5), key="timeseries_data")
    except Exception:
        log.warning("render_curvature: could not load data for %s", out_path, exc_info=True)
        return

    # Build {(worm_index, frame_number): detrended_curvature}
    smooth_win = max(3, int(fps * 0.3) | 1)
    detrend_win = max(5, int(fps * 2.0) | 1)
    ts_col = "timestamp" if "timestamp" in ts.columns else "frame_number"
    curv_lookup: dict[tuple[int, int], float] = {}
    for wi, grp in ts.groupby("worm_index"):
        frames = grp[ts_col].values
        raw = grp["curvature_midbody"].fillna(0.0).values
        smoothed = (pd.Series(raw)
                    .rolling(smooth_win, center=True, min_periods=1).mean().values)
        baseline = (pd.Series(smoothed)
                    .rolling(detrend_win, center=True, min_periods=1).mean().values)
        detrended = smoothed - baseline
        for frame_ts, val in zip(frames, detrended):
            curv_lookup[(int(wi), int(frame_ts))] = float(val)

    cap = cv2.VideoCapture(str(avi_path))
    if not cap.isOpened():
        log.warning("render_curvature: cannot open %s", avi_path.name)
        return

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    try:
        with _ffmpeg_writer(out_path, fps, w, h) as pipe:
            frame_num = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                for wi, skel_idx in frame_map.get(frame_num, []):
                    try:
                        curv = curv_lookup.get((wi, frame_num), 0.0)
                        if curv > 0:
                            col: tuple[int, int, int] = (0, 0, 220)    # red BGR
                        elif curv < 0:
                            col = (220, 0, 0)                           # blue BGR
                        else:
                            col = (128, 128, 128)                       # grey
                        _draw_skeleton(frame, skel, skel_idx, col, thickness=2)
                    except Exception:
                        log.debug("render_curvature: bad skeleton frame %d worm %d — skipping",
                                  frame_num, wi)
                pipe.write(frame.tobytes())
                frame_num += 1
        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_curvature failed writing %s", out_path, exc_info=True)
    finally:
        cap.release()


def render_sidebyside(
    avi_path: Path,
    masked_hdf5: Path,
    skeletons_hdf5: Path,
    out_path: Path,
    fps: float,
) -> None:
    """Render original frame (left) beside masked+tracked frame (right)."""
    import cv2
    import h5py

    log.info("Starting render: %s", out_path)

    if not _check_skel_col(skeletons_hdf5, "render_sidebyside"):
        return

    try:
        frame_map, skel = _load_skeleton_map(skeletons_hdf5)
    except Exception:
        log.warning("render_sidebyside: could not load skeleton data for %s", out_path, exc_info=True)
        return

    cap = cv2.VideoCapture(str(avi_path))
    if not cap.isOpened():
        log.warning("render_sidebyside: cannot open %s", avi_path.name)
        return

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    try:
        with h5py.File(masked_hdf5, "r") as mf:
            mask_ds = mf["mask"]
            n_masked = int(mask_ds.shape[0])
            with _ffmpeg_writer(out_path, fps, w * 2, h) as pipe:
                frame_num = 0
                while True:
                    ret, orig = cap.read()
                    if not ret:
                        break

                    if frame_num < n_masked:
                        gray = mask_ds[frame_num]   # one-frame lazy read
                        right = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                        if right.shape[:2] != (h, w):
                            right = cv2.resize(right, (w, h))
                    else:
                        right = np.zeros((h, w, 3), dtype=np.uint8)

                    for wi, skel_idx in frame_map.get(frame_num, []):
                        try:
                            _draw_skeleton(right, skel, skel_idx, _palette_color(wi))
                        except Exception:
                            log.debug("render_sidebyside: bad skeleton frame %d worm %d — skipping",
                                      frame_num, wi)

                    pipe.write(np.hstack([orig, right]).tobytes())
                    frame_num += 1
        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_sidebyside failed writing %s", out_path, exc_info=True)
    finally:
        cap.release()
