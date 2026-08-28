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

# Worm-ID label sizing for tracked + side-by-side renders. Tuned for the
# ~2028x1520 whole-plate frames; bump these two lines to retune. Shared by both
# the motility and crawling pipelines via _draw_skeleton.
_ID_FONT_SCALE: float = 1.4
_ID_FONT_THICKNESS: int = 3

# 12-colour BGR palette for stable per-worm (worm_index) colouring in the
# motility tracked / side-by-side renders. Indexed by worm_index % 12 so a
# worm's colour persists across every fragment belonging to it. Distinct from
# _PALETTE below, which colours individual worm_index_joined fragments for the
# crawling / legacy label path.
_WORM_PALETTE: list[tuple[int, int, int]] = [
    ( 66, 135, 245), ( 60, 180,  75), ( 48,  48, 230), ( 30, 200, 255),
    (205,  95,  40), (190,  70, 200), ( 80, 205, 205), ( 40, 110, 240),
    (150, 150,  40), ( 95,  60, 205), ( 60, 160,  95), (205, 135,  60),
]

# Faint grey marker for fragments tracked by Tierpsy but filtered out of the
# scored set (Phase 3): drawn at the fragment centroid, no label, no skeleton.
_FILTERED_MARKER_COLOR: tuple[int, int, int] = (135, 135, 135)
_FILTERED_MARKER_RADIUS: int = 4


def _worm_color(worm_index: int) -> tuple[int, int, int]:
    """Stable colour for a grouped worm_index (motility renders)."""
    return _WORM_PALETTE[int(worm_index) % len(_WORM_PALETTE)]


# ---------------------------------------------------------------------------
# Velocity-arrow + reversal/turn-event overlay (crawling pipeline).
#
# arrow_data maps a grouped worm_index to a dict of dense per-frame centroid /
# velocity arrays plus event-frame lists (see crawling_metrics._velocity_arrow_*):
#   {f0, x, y, vx, vy, reversal_event_frames, turn_event_frames}
# x/y/vx/vy are indexed by (frame_number - f0). The arrow starts at the centroid
# and points along the velocity vector, scaled by ARROW_RENDER_SCALE — tune this
# visually on day-0 output (45 px/frame-per-second ≈ 1.5x body-length for a
# typical-speed worm; it is a renderer constant, NOT read from crawling_metrics.py).
# Event markers pulse
# at the worm's current centroid from the event frame for _EVENT_MARKER_DURATION_S
# (min _EVENT_MARKER_MIN_FRAMES) so they survive across several rendered frames.
# ---------------------------------------------------------------------------
ARROW_RENDER_SCALE: float = 45.0
_ARROW_COLOR: tuple[int, int, int] = (0, 255, 255)   # yellow (BGR)
_ARROW_THICKNESS: int = 2
_ARROW_TIP_LENGTH: float = 0.3

_EVENT_MARKER_DURATION_S: float = 0.5
_EVENT_MARKER_MIN_FRAMES: int = 15
_REVERSAL_MARKER_COLOR: tuple[int, int, int] = (0, 0, 255)     # red (BGR)
_REVERSAL_MARKER_RADIUS: int = 12
_TURN_MARKER_COLOR: tuple[int, int, int] = (255, 255, 0)       # cyan (BGR)
_TURN_MARKER_RADIUS: int = 8
_MARKER_CIRCLE_OFFSET: tuple[int, int] = (15, -15)   # offset from centroid (px)
_MARKER_TEXT_OFFSET: tuple[int, int] = (30, -15)
_MARKER_FONT_SCALE: float = 0.9
_MARKER_FONT_THICKNESS: int = 2


def _prepare_arrow_worms(arrow_data: "dict[int, dict] | None") -> list[dict]:
    """
    Normalise arrow_data into a per-worm list with sorted event-frame arrays.

    Drops worms with no centroid track (f0 None / missing arrays). Returns [] when
    arrow_data is None/empty so callers can cheaply skip the overlay.
    """
    if not arrow_data:
        return []
    out: list[dict] = []
    for gi, ad in arrow_data.items():
        f0 = ad.get("f0")
        x = ad.get("x")
        if f0 is None or x is None or len(x) == 0:
            continue
        out.append({
            "f0": int(f0),
            "x": np.asarray(ad.get("x"), dtype=float),
            "y": np.asarray(ad.get("y"), dtype=float),
            "vx": np.asarray(ad.get("vx"), dtype=float),
            "vy": np.asarray(ad.get("vy"), dtype=float),
            "rev": np.sort(np.asarray(ad.get("reversal_event_frames") or [], dtype=np.int64)),
            "turn": np.sort(np.asarray(ad.get("turn_event_frames") or [], dtype=np.int64)),
        })
    return out


def _event_active(sorted_frames: np.ndarray, frame_num: int, duration: int) -> bool:
    """True if frame_num is within [ef, ef+duration) for the nearest prior event ef."""
    if len(sorted_frames) == 0:
        return False
    j = int(np.searchsorted(sorted_frames, frame_num, side="right")) - 1
    if j < 0:
        return False
    return (frame_num - int(sorted_frames[j])) < duration


def _draw_arrows_and_markers(
    frame: np.ndarray, arrow_worms: list[dict], frame_num: int,
    marker_duration: int, draw_markers: bool,
) -> None:
    """
    Draw the velocity arrow (always) and reversal/turn markers (when draw_markers)
    for every arrow_worm visible at frame_num, in-place on frame.
    """
    import cv2

    for w in arrow_worms:
        idx = frame_num - w["f0"]
        if idx < 0 or idx >= len(w["x"]):
            continue
        cx = w["x"][idx]; cy = w["y"][idx]
        vx = w["vx"][idx]; vy = w["vy"][idx]

        # Velocity arrow from the centroid along the velocity vector.
        if (np.isfinite(cx) and np.isfinite(cy)
                and np.isfinite(vx) and np.isfinite(vy)):
            p1 = (int(cx), int(cy))
            p2 = (int(cx + vx * ARROW_RENDER_SCALE), int(cy + vy * ARROW_RENDER_SCALE))
            cv2.arrowedLine(frame, p1, p2, _ARROW_COLOR, _ARROW_THICKNESS,
                            cv2.LINE_AA, 0, _ARROW_TIP_LENGTH)

        if not draw_markers or not (np.isfinite(cx) and np.isfinite(cy)):
            continue
        bx, by = int(cx), int(cy)
        # Reversal marker (red) then turn marker (cyan), offset off the centroid so
        # they don't obscure the skeleton.
        if _event_active(w["rev"], frame_num, marker_duration):
            cv2.circle(frame, (bx + _MARKER_CIRCLE_OFFSET[0], by + _MARKER_CIRCLE_OFFSET[1]),
                       _REVERSAL_MARKER_RADIUS, _REVERSAL_MARKER_COLOR, -1, cv2.LINE_AA)
            cv2.putText(frame, "REV",
                        (bx + _MARKER_TEXT_OFFSET[0], by + _MARKER_TEXT_OFFSET[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, _MARKER_FONT_SCALE,
                        _REVERSAL_MARKER_COLOR, _MARKER_FONT_THICKNESS, cv2.LINE_AA)
        if _event_active(w["turn"], frame_num, marker_duration):
            cv2.circle(frame, (bx + _MARKER_CIRCLE_OFFSET[0], by + _MARKER_CIRCLE_OFFSET[1]),
                       _TURN_MARKER_RADIUS, _TURN_MARKER_COLOR, -1, cv2.LINE_AA)
            cv2.putText(frame, "TURN",
                        (bx + _MARKER_TEXT_OFFSET[0], by + _MARKER_TEXT_OFFSET[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, _MARKER_FONT_SCALE,
                        _TURN_MARKER_COLOR, _MARKER_FONT_THICKNESS, cv2.LINE_AA)


def _event_marker_duration(fps: float) -> int:
    """Frames an event marker stays lit (see _EVENT_MARKER_* constants)."""
    return max(_EVENT_MARKER_MIN_FRAMES, int(round(_EVENT_MARKER_DURATION_S * fps)))


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


def _skel_centroid(skel: np.ndarray, row_idx: int) -> "tuple[int, int] | None":
    """Mean of the finite skeleton points for a row, or None if none are finite."""
    pts = skel[row_idx]
    finite = pts[np.isfinite(pts).all(axis=1)]
    if len(finite) == 0:
        return None
    return (int(finite[:, 0].mean()), int(finite[:, 1].mean()))


def _draw_faint_marker(frame: np.ndarray, pos: tuple[int, int]) -> None:
    """Draw a small faint grey dot for a filtered-out fragment (Phase 3)."""
    import cv2

    cv2.circle(frame, pos, _FILTERED_MARKER_RADIUS,
               _FILTERED_MARKER_COLOR, -1, cv2.LINE_AA)


def _draw_skeleton(
    frame: np.ndarray,
    skel: np.ndarray,
    row_idx: int,
    color: tuple[int, int, int],
    thickness: int = 1,
    label: str = "",
) -> None:
    """Draw a 49-point polyline on frame in-place, with an optional ID label.

    The label anchors at the head endpoint (skeleton point 0), falling back to
    the skeleton centroid when the head point is non-finite for this frame.
    """
    import cv2

    # Only the finite points, and only contiguous runs of them. A partly
    # skeletonised frame used to be cast whole: NaN -> int32 is undefined, it
    # lands on INT_MIN, and cv2 then drew a line from a real body point to a
    # coordinate far off screen. numpy said so every time ("invalid value
    # encountered in cast") and the warning was read as cosmetic.
    raw = skel[row_idx]
    finite = np.isfinite(raw).all(axis=1)
    if not finite.any():
        return
    idx = np.flatnonzero(finite)
    runs = np.split(idx, np.flatnonzero(np.diff(idx) != 1) + 1)
    for run in runs:
        if len(run) < 2:
            continue
        pts = raw[run].astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(frame, [pts], False, color, thickness, cv2.LINE_AA)
    if label:
        head_pt = skel[row_idx, 0]
        if np.isfinite(head_pt).all():
            anchor = (int(head_pt[0]), int(head_pt[1]))
        else:
            anchor = _skel_centroid(skel, row_idx)
        if anchor is not None:
            cv2.putText(frame, label, anchor,
                        cv2.FONT_HERSHEY_SIMPLEX, _ID_FONT_SCALE, color,
                        _ID_FONT_THICKNESS, cv2.LINE_AA)


# ---------------------------------------------------------------------------
# Public render functions
# ---------------------------------------------------------------------------

def resolve_worm_id(worm_index_map, tid: int, frame_num: int):
    """
    Which track does Tierpsy fragment `tid` belong to at `frame_num`?

    A worm_index_map value is either

        int                          - the whole fragment is that track
                                       (motility, and any crawling fragment
                                       that was never split)
        [(f0, f1, worm_index), ...]  - the fragment was cut at a collision, so
                                       different frame windows belong to
                                       different tracks (crawling)

    The second form exists because crawling's linker splits a fragment when two
    animals merge into one blob: the pieces either side become separate tracks
    with separate rows in the per_worm sheet, but they still share one Tierpsy
    worm_index_joined. Keying the render on the id alone would draw both halves
    under whichever track was written last, so the number on screen would not
    match the number in the spreadsheet.

    Returns None when the fragment is not drawn at this frame - filtered out,
    or inside a dropped merge episode.
    """
    v = worm_index_map.get(tid)
    if v is None:
        return None
    if isinstance(v, (int, np.integer)):
        return int(v)
    for f0, f1, wi in v:
        if f0 <= frame_num <= f1:
            return int(wi)
    return None


def render_tracked(
    avi_path: Path,
    skeletons_hdf5: Path,
    out_path: Path,
    fps: float,
    kept_ids: "set[int] | None" = None,
    worm_index_map: "dict | None" = None,
    arrow_data: "dict[int, dict] | None" = None,
) -> None:
    """
    Render the AVI with skeleton polylines and worm-index labels.

    kept_ids, when given, restricts drawing to those worm_index_joined values
    (used by the crawling pipeline so renders match the quality filter). When
    None (the motility default) every tracked worm is drawn.

    worm_index_map (motility), when given, maps each worm_index_joined fragment
    to its stable grouped worm_index. Mapped fragments are drawn in that worm's
    persistent palette colour and labelled with the worm_index (matching the
    Excel); fragments absent from the map are filtered-out, so they get a faint
    grey centroid marker only (Phase 3). Takes precedence over kept_ids.

    arrow_data (crawling), when given, draws a per-frame velocity arrow plus
    reversal/turn event markers for each kept grouped worm — see
    _draw_arrows_and_markers / _prepare_arrow_worms. Independent of the skeleton
    frame_map (arrows use the centroid track, so they render even on frames where
    the skeleton dropped out).
    """
    import cv2

    log.info("Starting render: %s", out_path)

    if not _check_skel_col(skeletons_hdf5, "render_tracked"):
        return

    try:
        frame_map, skel = _load_skeleton_map(skeletons_hdf5)
    except Exception:
        log.warning("render_tracked: could not load skeleton data for %s", out_path, exc_info=True)
        return

    arrow_worms = _prepare_arrow_worms(arrow_data)
    marker_duration = _event_marker_duration(fps)

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
                for tid, skel_idx in frame_map.get(frame_num, []):
                    if worm_index_map is not None:
                        worm_id = resolve_worm_id(
                            worm_index_map, tid, frame_num)
                        if worm_id is None:
                            pos = _skel_centroid(skel, skel_idx)
                            if pos is not None:
                                _draw_faint_marker(frame, pos)
                            continue
                        try:
                            _draw_skeleton(frame, skel, skel_idx,
                                           _worm_color(worm_id), label=str(worm_id))
                        except Exception:
                            log.debug("render_tracked: bad skeleton frame %d worm %d — skipping",
                                      frame_num, worm_id)
                        continue
                    if kept_ids is not None and tid not in kept_ids:
                        continue
                    try:
                        _draw_skeleton(frame, skel, skel_idx,
                                       _palette_color(tid), label=str(tid))
                    except Exception:
                        log.debug("render_tracked: bad skeleton frame %d worm %d — skipping",
                                  frame_num, tid)
                # Velocity arrows + reversal/turn markers on top of the skeletons.
                if arrow_worms:
                    _draw_arrows_and_markers(frame, arrow_worms, frame_num,
                                             marker_duration, draw_markers=True)
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
    worm_ids: list[int],
    repr_worm_index: int,
    fps: float,
    out_path: Path,
    head_angle_prominence: float = 0.30,
) -> None:
    """
    Two-panel MP4 for one worm (possibly spanning multiple Tierpsy fragments):
      Left  — curvature trace drawing in left-to-right at native fps.
      Right — cropped masked video centred on the worm with skeleton overlay.
    """
    import cv2
    import h5py
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    log.info("Starting render: worms %s → %s", worm_ids, out_path)

    if not _check_skel_col(skeletons_hdf5, "render_per_worm_trace"):
        return

    try:
        frame_map, skel = _load_skeleton_map(skeletons_hdf5)
    except Exception:
        log.warning("render_per_worm_trace: could not load skeleton data for worms %s",
                    worm_ids, exc_info=True)
        return

    # Build worm_frame_map from all member TIDs; lower TID wins on overlap.
    worm_ids_set = set(worm_ids)
    worm_frame_map: dict[int, int] = {}
    for fn, entries in frame_map.items():
        for wi_entry, skel_idx in sorted(entries, key=lambda x: x[0]):
            if wi_entry in worm_ids_set:
                worm_frame_map[fn] = skel_idx
                break

    # Compute combined head-angle signal across all member TIDs.
    try:
        _traj = pd.read_hdf(str(featuresN_hdf5), key="trajectories_data")
        with h5py.File(str(featuresN_hdf5), "r") as _fh:
            _skel_all = _fh["coordinates"]["skeletons"][:]
        _frame_col = "timestamp_raw" if "timestamp_raw" in _traj.columns else "frame_number"
        from analysis.analysis_csv import compute_head_angle_signal
        _combined_fn: list[int] = []
        _combined_dt: list[float] = []
        _all_peak_fns: list[int] = []
        for tid in worm_ids:
            _worm_traj = (_traj[_traj["worm_index_joined"] == tid]
                          .sort_values(_frame_col).reset_index(drop=True))
            _sig = compute_head_angle_signal(_worm_traj, _skel_all, fps, head_angle_prominence)
            if _sig is None:
                continue
            _combined_fn.extend(_sig["frame_nums"].tolist())
            _combined_dt.extend(_sig["detrended"].tolist())
            _all_peak_fns.extend(_sig["frame_nums"][_sig["pos_peaks"]].tolist())
            _all_peak_fns.extend(_sig["frame_nums"][_sig["neg_peaks"]].tolist())
    except Exception:
        log.warning("render_per_worm_trace: could not compute head-angle signal for worms %s",
                    worm_ids, exc_info=True)
        return

    if not _combined_fn:
        log.warning("render_per_worm_trace: no valid head-angle signal for worms %s", worm_ids)
        return

    _sort = np.argsort(_combined_fn)
    frames_col = np.array(_combined_fn)[_sort]
    detrended = np.array(_combined_dt)[_sort]
    t_sec = frames_col / fps  # absolute video time
    _peak_fn_arr = np.array(sorted(set(_all_peak_fns)), dtype=int)
    peak_indices = (np.where(np.isin(frames_col, _peak_fn_arr))[0]
                    if len(_peak_fn_arr) else np.array([], dtype=int))

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
        log.warning("render_per_worm_trace: no clean skeleton bounding boxes for worms %s",
                    worm_ids)
        return

    body_len = float(np.median(body_lengths))
    crop_raw = body_len * 1.5          # guard at the cast site — not upstream
    if not np.isfinite(crop_raw):
        log.warning("render_per_worm_trace: non-finite crop size for worms %s (body_len=%g)",
                    worm_ids, body_len)
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
    t_min_plot = float(t_sec[0]) if len(t_sec) else 0.0
    t_max_plot = float(t_sec[-1]) if len(t_sec) else t_min_plot + 1.0
    ax.set_xlim(t_min_plot - 0.5, t_max_plot + 0.5)
    ax.set_ylim(-y_abs - y_margin, y_abs + y_margin)
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    tick_start = int(t_min_plot / 5) * 5
    ax.set_xticks(np.arange(tick_start, t_max_plot + 5, 5))
    ax.set_xlabel("Time (s)", fontsize=7)
    ax.set_ylabel("Head angle (rad)", fontsize=7)
    ax.tick_params(labelsize=6)
    (line,) = ax.plot([], [], color="#1976d2", linewidth=0.8)
    peak_scatter = ax.scatter([], [], color="red", s=10, zorder=5)
    fig.tight_layout(pad=0.5)
    fig.canvas.draw()
    canvas_w, canvas_h = fig.canvas.get_width_height()

    color = _palette_color(repr_worm_index)
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

        # Constrain render to the worm's actual tracked frame range.
        frame_min = min(worm_frame_map.keys()) if worm_frame_map else 0
        frame_max = min(max(worm_frame_map.keys()) if worm_frame_map else n_masked - 1,
                        n_masked - 1)

        with _ffmpeg_writer(out_path, fps, total_w, panel_h) as pipe:
            for frame_num in range(frame_min, frame_max + 1):
                try:
                    # Left panel — cumulative trace up to this video frame
                    idx = int(np.searchsorted(frames_col, frame_num + 1))
                    line.set_data(t_sec[:idx], detrended[:idx])
                    vis_peaks = peak_indices[peak_indices < idx]
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
                                "render_per_worm_trace: bad skeleton at frame %d — skipping",
                                frame_num,
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
        log.warning("render_per_worm_trace failed for worms %s", worm_ids, exc_info=True)
    finally:
        mf.close()
        plt.close(fig)


def render_sidebyside(
    avi_path: Path,
    masked_hdf5: Path,
    skeletons_hdf5: Path,
    out_path: Path,
    fps: float,
    kept_ids: "set[int] | None" = None,
    worm_index_map: "dict | None" = None,
) -> None:
    """
    Render original frame (left) beside masked+tracked frame (right).

    kept_ids, when given, restricts drawing to those worm_index_joined values
    (crawling quality filter). None (motility default) draws every worm.

    worm_index_map (motility) behaves as in render_tracked: mapped fragments are
    drawn in their stable worm colour and labelled with the worm_index, filtered
    fragments get a faint grey marker. Takes precedence over kept_ids.
    """
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

                    for tid, skel_idx in frame_map.get(frame_num, []):
                        if worm_index_map is not None:
                            worm_id = resolve_worm_id(
                                worm_index_map, tid, frame_num)
                            if worm_id is None:
                                pos = _skel_centroid(skel, skel_idx)
                                if pos is not None:
                                    _draw_faint_marker(right, pos)
                                continue
                            try:
                                _draw_skeleton(right, skel, skel_idx,
                                               _worm_color(worm_id), label=str(worm_id))
                            except Exception:
                                log.debug("render_sidebyside: bad skeleton frame %d worm %d — skipping",
                                          frame_num, worm_id)
                            continue
                        if kept_ids is not None and tid not in kept_ids:
                            continue
                        try:
                            _draw_skeleton(right, skel, skel_idx, _palette_color(tid),
                                           label=str(tid))
                        except Exception:
                            log.debug("render_sidebyside: bad skeleton frame %d worm %d — skipping",
                                      frame_num, tid)

                    pipe.write(np.hstack([orig, right]).tobytes())
                    frame_num += 1
        log.info("Render complete: %s", out_path)
    except Exception:
        log.warning("render_sidebyside failed writing %s", out_path, exc_info=True)
    finally:
        cap.release()
