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


def render_per_worm_trace(
    masked_hdf5: Path,
    skeletons_hdf5: Path,
    featuresN_hdf5: Path,
    worm_index: int,
    fps: float,
    out_path: Path,
    head_angle_prominence: float = 0.30,
) -> None:
    """
    Two-panel MP4 for one fully-tracked worm:
      Left  — curvature trace drawing in left-to-right at native fps.
      Right — cropped masked video centred on the worm with skeleton overlay.
    """
    import cv2
    import h5py
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    log.info("Starting render: worm %d → %s", worm_index, out_path)

    if not _check_skel_col(skeletons_hdf5, "render_per_worm_trace"):
        return

    try:
        frame_map, skel = _load_skeleton_map(skeletons_hdf5)
    except Exception:
        log.warning("render_per_worm_trace: could not load skeleton data for worm %d",
                    worm_index, exc_info=True)
        return

    # Filter to this worm only: frame_num → skel_row_idx
    # _load_skeleton_map already skipped NaN frame_number / worm_index_joined rows.
    worm_frame_map: dict[int, int] = {}
    for fn, entries in frame_map.items():
        for wi, skel_idx in entries:
            if wi == worm_index:
                worm_frame_map[fn] = skel_idx
                break

    # Compute head-angle signal
    try:
        _traj = pd.read_hdf(str(featuresN_hdf5), key="trajectories_data")
        with h5py.File(str(featuresN_hdf5), "r") as _fh:
            _skel_all = _fh["coordinates"]["skeletons"][:]
        _frame_col = "timestamp_raw" if "timestamp_raw" in _traj.columns else "frame_number"
        _worm_traj = (_traj[_traj["worm_index_joined"] == worm_index]
                      .sort_values(_frame_col).reset_index(drop=True))
        from analysis.analysis_csv import compute_head_angle_signal
        signal = compute_head_angle_signal(_worm_traj, _skel_all, fps, head_angle_prominence)
    except Exception:
        log.warning("render_per_worm_trace: could not compute head-angle signal for worm %d",
                    worm_index, exc_info=True)
        return

    if signal is None:
        log.warning("render_per_worm_trace: no valid head-angle signal for worm %d", worm_index)
        return

    frames_col = signal["frame_nums"]
    detrended = signal["detrended"]
    t_sec = frames_col / fps
    pos_peaks = signal["pos_peaks"]
    neg_peaks = signal["neg_peaks"]

    # Compute crop size from skeleton bounding-box diagonals.
    # Guard: NaN coords produce NaN from min/max, which propagates silently through
    # np.hypot → median → int() crash.  Skip any skeleton row with NaN coords.
    body_lengths: list[float] = []
    for fn, skel_idx in worm_frame_map.items():
        try:
            pts = skel[skel_idx]
            if not np.isfinite(pts).all():  # rejects NaN *and* ±inf (inf−inf = nan → int crash)
                continue
            body_lengths.append(float(np.hypot(
                pts[:, 0].max() - pts[:, 0].min(),
                pts[:, 1].max() - pts[:, 1].min(),
            )))
        except Exception:
            log.debug("render_per_worm_trace: bad bounding box at skel row %d — skipping",
                      skel_idx)

    if not body_lengths:
        log.warning("render_per_worm_trace: no clean skeleton bounding boxes for worm %d",
                    worm_index)
        return

    body_len = float(np.median(body_lengths))
    crop_raw = body_len * 1.5          # guard at the cast site — not upstream
    if not np.isfinite(crop_raw):
        log.warning("render_per_worm_trace: non-finite crop size for worm %d (body_len=%g)",
                    worm_index, body_len)
        return
    crop_size = max(int(crop_raw), 80)
    if crop_size % 2:
        crop_size += 1

    panel_h = crop_size
    panel_w_right = crop_size
    panel_w_left = crop_size * 2
    if panel_w_left % 2:
        panel_w_left += 1
    total_w = panel_w_left + panel_w_right

    # Build matplotlib left-panel figure (reused each frame)
    dpi = 100
    fig, ax = plt.subplots(figsize=(panel_w_left / dpi, panel_h / dpi), dpi=dpi)
    fig.patch.set_facecolor("white")

    finite_vals = detrended[np.isfinite(detrended)]
    y_abs = float(np.max(np.abs(finite_vals))) if len(finite_vals) else 1.0
    y_margin = y_abs * 0.1 + 1e-6
    t_max = float(t_sec[-1]) if len(t_sec) else 1.0
    ax.set_xlim(0, t_max)
    ax.set_ylim(-y_abs - y_margin, y_abs + y_margin)
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax.set_xticks(np.arange(0, t_max + 5, 5))
    ax.set_xlabel("Time (s)", fontsize=7)
    ax.set_ylabel("Head angle (rad)", fontsize=7)
    ax.tick_params(labelsize=6)
    (line,) = ax.plot([], [], color="#1976d2", linewidth=0.8)
    peak_scatter = ax.scatter([], [], color="red", s=10, zorder=5)
    fig.tight_layout(pad=0.5)
    fig.canvas.draw()
    canvas_w, canvas_h = fig.canvas.get_width_height()

    color = _palette_color(worm_index)
    last_center: tuple[int, int] = (0, 0)  # updated once a clean skeleton is seen

    try:
        mf = h5py.File(masked_hdf5, "r")
    except Exception:
        log.warning("render_per_worm_trace: cannot open %s", masked_hdf5, exc_info=True)
        plt.close(fig)
        return

    try:
        mask_ds = mf["mask"]
        n_masked = int(mask_ds.shape[0])
        mh = int(mask_ds.shape[1])
        mw = int(mask_ds.shape[2])
        last_center = (mw // 2, mh // 2)

        with _ffmpeg_writer(out_path, fps, total_w, panel_h) as pipe:
            for frame_num in range(n_masked):
                try:
                    # Left panel — cumulative trace up to this video frame
                    idx = int(np.searchsorted(frames_col, frame_num + 1))
                    line.set_data(t_sec[:idx], detrended[:idx])
                    vis_peaks = np.concatenate(
                        [pos_peaks[pos_peaks < idx], neg_peaks[neg_peaks < idx]]
                    )
                    if len(vis_peaks):
                        peak_scatter.set_offsets(
                            np.column_stack([t_sec[vis_peaks], detrended[vis_peaks]])
                        )
                    else:
                        peak_scatter.set_offsets(np.empty((0, 2)))
                    fig.canvas.draw()
                    buf = np.frombuffer(fig.canvas.buffer_rgba(), dtype=np.uint8)
                    left_rgba = buf.reshape(canvas_h, canvas_w, 4)
                    left_bgr = cv2.cvtColor(left_rgba, cv2.COLOR_RGBA2BGR)
                    if left_bgr.shape[0] != panel_h or left_bgr.shape[1] != panel_w_left:
                        left_bgr = cv2.resize(left_bgr, (panel_w_left, panel_h))

                    # Right panel — masked frame + skeleton overlay + crop
                    gray = mask_ds[frame_num]
                    right = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                    if frame_num in worm_frame_map:
                        skel_row = worm_frame_map[frame_num]
                        try:
                            pts = skel[skel_row]
                            if not np.isfinite(pts).all():  # rejects NaN and ±inf
                                log.debug(
                                    "render_per_worm_trace: non-finite coords at frame %d — skipping overlay",
                                    frame_num,
                                )
                            else:
                                # Guard at each int() cast site — don't rely on the isfinite
                                # check above alone; inf coords can still produce nan via
                                # subtraction (e.g. min == max == inf → inf − inf = nan).
                                cx_f = (pts[:, 0].min() + pts[:, 0].max()) / 2
                                cy_f = (pts[:, 1].min() + pts[:, 1].max()) / 2
                                if not (np.isfinite(cx_f) and np.isfinite(cy_f)):
                                    log.debug(
                                        "render_per_worm_trace: non-finite centre at frame %d — skipping overlay",
                                        frame_num,
                                    )
                                else:
                                    _draw_skeleton(right, skel, skel_row, color, thickness=1)
                                    last_center = (int(cx_f), int(cy_f))
                        except Exception:
                            log.debug(
                                "render_per_worm_trace: bad skeleton at frame %d worm %d — skipping",
                                frame_num, worm_index,
                            )

                    cx, cy = last_center
                    half = crop_size // 2
                    x1 = max(0, cx - half)
                    y1 = max(0, cy - half)
                    x2 = min(mw, x1 + crop_size)
                    y2 = min(mh, y1 + crop_size)
                    x1 = max(0, x2 - crop_size)
                    y1 = max(0, y2 - crop_size)
                    crop = right[y1:y2, x1:x2]
                    if crop.shape[0] != crop_size or crop.shape[1] != crop_size:
                        crop = cv2.resize(crop, (crop_size, crop_size))

                    pipe.write(np.hstack([left_bgr, crop]).tobytes())

                except (ValueError, TypeError) as exc:
                    log.debug("render_per_worm_trace: per-frame error at frame %d: %s",
                              frame_num, exc)
                    pipe.write(np.zeros((panel_h, total_w, 3), dtype=np.uint8).tobytes())

        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_per_worm_trace failed for worm %d", worm_index, exc_info=True)
    finally:
        mf.close()
        plt.close(fig)


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
