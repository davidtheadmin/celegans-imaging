"""
Motility analysis: bend counting + fragment-grouping pipeline.

Pipeline config
---------------
All thresholds are starting defaults; tune after first run on validation videos.
"""
import logging
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline configuration — tune these after reviewing analysis_log.json outputs
# ---------------------------------------------------------------------------
DISTANCE_THRESHOLD_PIXELS: float = 50       # max centroid gap for fragment adjacency
TIME_GAP_THRESHOLD_SECONDS: float = 5.0     # max wall-clock gap for adjacency (generous; covers most curls)
FLICKER_WINDOW_SECONDS: float = 0.5         # rolling-std window for skeleton-length flicker detection
FLICKER_STD_THRESHOLD_PIXELS: float = 20    # rolling-std ceiling; expect to tighten after first run
MIN_OBSERVATION_TIME_SECONDS: float = 10.0  # drop worm if total clean observation time is below this
COLLISION_WORM_COUNT_CAP: int = 3           # max worms extracted from one collision cluster
DEBRIS_DISPLACEMENT_PIXELS: float = 8.0    # debris filter: max displacement
DEBRIS_BPM_THRESHOLD: float = 5.0          # debris filter: max BPM
DEBRIS_LENGTH_CV_MIN: float = 0.10         # debris filter: flickery if length_cv exceeds this
DEBRIS_SOLIDITY_MIN: float = 0.6           # debris filter: blob-shaped if solidity_median exceeds this
DEBRIS_SPEED_MAX: float = 10.0             # debris filter: high speed = real worm (safety gate)


# ---------------------------------------------------------------------------
# Bend counter — UNCHANGED from v1. Black box: takes a clean angle time-series,
# returns peak counts. Do not modify.
# ---------------------------------------------------------------------------

def _detrend(sig: np.ndarray, fps: float) -> np.ndarray:
    """Smooth (0.3 s window) then subtract slow rolling baseline (2.0 s window)."""
    smooth_win = max(3, int(fps * 0.3) | 1)
    smoothed = pd.Series(sig).rolling(smooth_win, center=True, min_periods=1).mean().values
    baseline_win = max(5, int(fps * 2.0) | 1)
    baseline = pd.Series(smoothed).rolling(baseline_win, center=True, min_periods=1).mean().values
    return smoothed - baseline


def compute_head_angle_signal(
    worm_traj: pd.DataFrame,
    skel_all: np.ndarray,
    fps: float,
    prominence: float = 0.30,
) -> "dict | None":
    """
    Compute the head-angle signal for one worm track.

    worm_traj  — trajectories_data rows for this worm, sorted by the frame-number
                 column; must have 'skeleton_id' and a frame-number column.
    skel_all   — full coordinates/skeletons array from _featuresN.hdf5 (N, 49, 2).
    prominence — scipy find_peaks prominence threshold in radians.

    Returns a dict with keys:
        frame_nums  — (M,) int array of video frame numbers for valid angle rows
        detrended   — (M,) float array of detrended head-angle signal
        pos_peaks   — indices into detrended where positive peaks were detected
        neg_peaks   — indices into detrended where negative peaks were detected
        n_valid     — M (number of valid-angle frames, used for duration calculation)

    Returns None if fewer than 10 valid samples.
    """
    frame_col = "timestamp_raw" if "timestamp_raw" in worm_traj.columns else "frame_number"
    n_rows = len(worm_traj)
    angles = np.full(n_rows, np.nan)
    frame_nums_raw = worm_traj[frame_col].values
    skel_id_col = worm_traj["skeleton_id"].values

    for i in range(n_rows):
        sid_raw = skel_id_col[i]
        if not np.isfinite(float(sid_raw)):
            continue
        sid = int(sid_raw)
        if sid < 0 or sid >= len(skel_all):
            continue
        skel = skel_all[sid]
        if not np.isfinite(skel).all():
            continue
        head_vec = skel[0] - skel[5]
        body_vec = skel[20] - skel[30]
        angles[i] = np.arctan2(
            head_vec[0] * body_vec[1] - head_vec[1] * body_vec[0],
            head_vec[0] * body_vec[0] + head_vec[1] * body_vec[1],
        )

    valid_mask = np.isfinite(angles)
    valid_angles = angles[valid_mask]
    if len(valid_angles) < 10:
        return None

    fn_finite = np.isfinite(frame_nums_raw[valid_mask].astype(float))
    valid_angles = valid_angles[fn_finite]
    valid_frame_nums = frame_nums_raw[valid_mask][fn_finite].astype(int)
    if len(valid_angles) < 10:
        return None

    detrended = _detrend(valid_angles, fps)
    pos_peaks, _ = find_peaks(detrended, prominence=prominence)
    neg_peaks, _ = find_peaks(-detrended, prominence=prominence)

    return {
        "frame_nums": valid_frame_nums,
        "detrended": detrended,
        "pos_peaks": pos_peaks,
        "neg_peaks": neg_peaks,
        "n_valid": len(valid_angles),
    }


def bends_per_minute(signal: dict, fps: float) -> float:
    """Convert a head-angle signal dict to bends per minute."""
    half_bends = len(signal["pos_peaks"]) + len(signal["neg_peaks"])
    bends = half_bends / 2.0
    duration_min = signal["n_valid"] / fps / 60.0
    if duration_min < 1e-9:
        return 0.0
    return bends / duration_min


def bend_interval_cv(all_peak_frames: np.ndarray, fps: float) -> float:
    """
    Coefficient of variation of inter-peak intervals (seconds).
    Returns NaN when fewer than 3 peaks are present (need ≥2 intervals for stdev).
    Lower = more regular bending rhythm.
    """
    if len(all_peak_frames) < 3:
        return float("nan")
    intervals = np.diff(np.sort(all_peak_frames)) / fps
    mean_interval = float(np.mean(intervals))
    if mean_interval < 1e-9:
        return float("nan")
    return float(np.std(intervals) / mean_interval)


# ---------------------------------------------------------------------------
# Displacement helper
# ---------------------------------------------------------------------------

def _displacement_px(track_dfs: List[pd.DataFrame]) -> float:
    """
    Max pairwise centroid distance across all provided track DataFrames.
    Uses a random subsample (≤ 300 points) for speed.
    """
    xs, ys = [], []
    for df in track_dfs:
        if "coord_x" in df.columns and "coord_y" in df.columns:
            xs.append(df["coord_x"].values)
            ys.append(df["coord_y"].values)
    if not xs:
        return 0.0
    all_x = np.concatenate(xs)
    all_y = np.concatenate(ys)
    valid = np.isfinite(all_x) & np.isfinite(all_y)
    all_x, all_y = all_x[valid], all_y[valid]
    if len(all_x) < 2:
        return 0.0
    if len(all_x) > 300:
        idx = np.random.choice(len(all_x), 300, replace=False)
        all_x, all_y = all_x[idx], all_y[idx]
    pts = np.stack([all_x, all_y], axis=1)
    diffs = pts[:, None, :] - pts[None, :, :]
    return float(np.sqrt((diffs ** 2).sum(axis=2)).max())


# ---------------------------------------------------------------------------
# Shape-stability metrics (from timeseries_data / blob_features)
# ---------------------------------------------------------------------------

def _shape_metrics(
    track_ids: list,
    traj: pd.DataFrame,
    timeseries_df: "pd.DataFrame | None",
    blob_feats: "pd.DataFrame | None",
) -> "tuple[float, float, float]":
    """
    Compute (length_cv, solidity_median, speed_median_abs) for a worm group.
    Returns NaN for any metric with insufficient data.
    """
    length_cv = float("nan")
    speed_median_abs = float("nan")
    solidity_median = float("nan")

    if timeseries_df is not None and "worm_index" in timeseries_df.columns:
        ts_sub = timeseries_df[timeseries_df["worm_index"].isin(track_ids)]
        if "length" in ts_sub.columns:
            lengths = ts_sub["length"].values.astype(float)
            lengths = lengths[np.isfinite(lengths)]
            if len(lengths) >= 10 and np.mean(lengths) > 0:
                length_cv = float(np.std(lengths) / np.mean(lengths))
        if "speed" in ts_sub.columns:
            speeds = np.abs(ts_sub["speed"].values.astype(float))
            speeds = speeds[np.isfinite(speeds)]
            if len(speeds) > 0:
                speed_median_abs = float(np.median(speeds))

    if blob_feats is not None and "solidity" in blob_feats.columns:
        n = min(len(traj), len(blob_feats))
        mask = traj["worm_index_joined"].iloc[:n].isin(track_ids).values
        solids = blob_feats["solidity"].values[:n][mask].astype(float)
        solids = solids[np.isfinite(solids)]
        if len(solids) > 0:
            solidity_median = float(np.median(solids))

    return length_cv, solidity_median, speed_median_abs


# ---------------------------------------------------------------------------
# Collision sub-track selection helpers
# ---------------------------------------------------------------------------

def _max_concurrent(subtracks: List[pd.DataFrame], frame_col: str) -> int:
    """Return the maximum number of sub-tracks simultaneously active at any frame."""
    if not subtracks:
        return 0
    check_frames: set = set()
    for st in subtracks:
        check_frames.add(int(st[frame_col].iloc[0]))
        check_frames.add(int(st[frame_col].iloc[-1]))
    best = 0
    for f in check_frames:
        count = sum(
            1 for st in subtracks
            if int(st[frame_col].iloc[0]) <= f <= int(st[frame_col].iloc[-1])
        )
        best = max(best, count)
    return best


def _select_collision_subtracks(
    subtracks: List[pd.DataFrame], frame_col: str, N: int
) -> List[pd.DataFrame]:
    """
    Select up to N sub-tracks that all share at least one concurrent frame,
    maximising combined frame count.
    """
    if len(subtracks) <= N:
        return list(subtracks)
    check_frames: set = set()
    for st in subtracks:
        check_frames.add(int(st[frame_col].iloc[0]))
        check_frames.add(int(st[frame_col].iloc[-1]))
    best_indices: "list[int] | None" = None
    best_total = -1
    for f in check_frames:
        active = [
            i for i, st in enumerate(subtracks)
            if int(st[frame_col].iloc[0]) <= f <= int(st[frame_col].iloc[-1])
        ]
        if len(active) < N:
            continue
        active.sort(key=lambda i: -len(subtracks[i]))
        selected = active[:N]
        total = sum(len(subtracks[i]) for i in selected)
        if total > best_total:
            best_total = total
            best_indices = selected
    if best_indices is None:
        sorted_by_len = sorted(range(len(subtracks)), key=lambda i: -len(subtracks[i]))
        best_indices = sorted_by_len[:N]
    return [subtracks[i] for i in best_indices]


# ---------------------------------------------------------------------------
# Per-track drop logging helpers
# ---------------------------------------------------------------------------

def _st_tierpsy_id(st: pd.DataFrame, group) -> int:
    """Return the Tierpsy worm_index_joined for a clean sub-track."""
    if "worm_index_joined" in st.columns:
        ids = st["worm_index_joined"].unique()
        if len(ids) == 1:
            return int(ids[0])
    return group.repr_track_id


def _log_drop(
    dropped_tracks: list,
    flicker_stats_by_tid: dict,
    tierpsy_id: int,
    reason: str,
    group,
    displacement_px: float = 0.0,
    bpm: "float | None" = None,
) -> None:
    fl = flicker_stats_by_tid.get(tierpsy_id, {})
    dropped_tracks.append({
        "tierpsy_id": tierpsy_id,
        "reason": reason,
        "longest_clean_duration_s": round(fl.get("longest_clean_duration_s", 0.0), 3),
        "total_flicker_frames": fl.get("total_flicker_frames", 0),
        "n_fragments_in_group": group.fragment_count,
        "group_id": group.virtual_worm_id,
        "displacement_px": round(displacement_px, 3),
        "bpm": (round(bpm, 3) if bpm is not None else None),
    })


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------

def read_fragments(
    hdf5_path: Path,
    fps: float,
    condition: str,
    plate: str,
    long_threshold_s: float = 5.0,
    head_angle_prominence: float = 0.30,
) -> "tuple[list[dict], dict]":
    """
    Run the full fragment-grouping + flicker-filter pipeline on one _featuresN.hdf5.

    Returns (per_worm_rows, analysis_log).

    Per-worm rows contain all previous columns plus:
        curl_count, fragment_count, valid_frac, group_classification, repr_tierpsy_id
    """
    from analysis.fragment_grouping import group_fragments
    from analysis.flicker_filter import filter_track
    import h5py

    # ---- load HDF5 ----
    try:
        traj = pd.read_hdf(str(hdf5_path), key="trajectories_data")
    except Exception as exc:
        log.error("Failed to read trajectories_data from %s: %s", hdf5_path, exc)
        return [], _empty_log()

    try:
        with h5py.File(str(hdf5_path), "r") as fh:
            skel_all = fh["coordinates"]["skeletons"][:]
    except Exception as exc:
        log.error("Failed to read skeletons from %s: %s", hdf5_path, exc)
        return [], _empty_log()

    timeseries_df = None
    blob_feats = None
    try:
        timeseries_df = pd.read_hdf(str(hdf5_path), key="timeseries_data")
    except Exception:
        pass
    try:
        blob_feats = pd.read_hdf(str(hdf5_path), key="blob_features")
    except Exception:
        pass

    frame_col = "timestamp_raw" if "timestamp_raw" in traj.columns else "frame_number"
    total_frames = max(int(traj[frame_col].max()) + 1, 1)

    input_track_count = int(traj["worm_index_joined"].nunique())

    # ---- Step 1 + 2: group and classify fragments ----
    groups = group_fragments(traj, fps, DISTANCE_THRESHOLD_PIXELS, TIME_GAP_THRESHOLD_SECONDS)

    n_curl = sum(1 for g in groups if g.classification == "curl")
    n_collision = sum(1 for g in groups if g.classification == "collision")

    # ---- Steps 3–5: per-group processing ----
    rows: list[dict] = []

    # Logging accumulators
    total_flicker_frames = 0
    tracks_with_any_flicker = 0
    tracks_dropped_flicker = 0
    dropped_curl_too_short = 0
    dropped_collision_too_short = 0
    fragment_count_dist: dict[int, int] = {}
    multi_worm_clusters = 0
    collision_worms_per_cluster: dict[int, int] = {}
    next_worm_idx = 0
    dropped_tracks: list[dict] = []
    flicker_stats_by_tid: dict[int, dict] = {}

    # Pre-index traj by track_id for fast lookup
    traj_by_track: dict[int, pd.DataFrame] = {
        int(tid): grp.sort_values(frame_col).reset_index(drop=True)
        for tid, grp in traj.groupby("worm_index_joined")
    }

    for group in groups:
        fc = group.fragment_count
        fragment_count_dist[fc] = fragment_count_dist.get(fc, 0) + 1

        # ---- Step 3: flicker-filter every track in the group ----
        per_track_filtered: dict[int, dict] = {}
        for tid in group.track_ids:
            track_df = traj_by_track.get(tid)
            if track_df is None or len(track_df) == 0:
                per_track_filtered[tid] = {
                    "clean_subtracks": [],
                    "flicker_frame_count": 0,
                    "flicker_stretch_count": 0,
                    "longest_clean_s": 0.0,
                    "total_frames": 0,
                }
                flicker_stats_by_tid[tid] = {"longest_clean_duration_s": 0.0, "total_flicker_frames": 0}
                continue
            result = filter_track(track_df, skel_all, fps, FLICKER_WINDOW_SECONDS, FLICKER_STD_THRESHOLD_PIXELS)
            per_track_filtered[tid] = result
            total_flicker_frames += result["flicker_frame_count"]
            if result["flicker_frame_count"] > 0:
                tracks_with_any_flicker += 1
            flicker_stats_by_tid[tid] = {
                "longest_clean_duration_s": result["longest_clean_s"],
                "total_flicker_frames": result["flicker_frame_count"],
            }

        # Collect all clean sub-tracks for this group
        all_clean_subtracks: list[pd.DataFrame] = []
        for tid in group.track_ids:
            all_clean_subtracks.extend(per_track_filtered[tid]["clean_subtracks"])

        total_clean_frames = sum(len(st) for st in all_clean_subtracks)
        total_clean_s = total_clean_frames / fps

        # ---- Steps 4+5: drop short worms and compute metrics ----
        if group.classification == "curl":
            if total_clean_s < MIN_OBSERVATION_TIME_SECONDS:
                if total_clean_frames == 0:
                    tracks_dropped_flicker += 1
                    drop_reason = "flicker_killed_track"
                else:
                    dropped_curl_too_short += 1
                    drop_reason = "curl_too_short"
                _group_tdfs = [traj_by_track[tid] for tid in group.track_ids if tid in traj_by_track]
                _group_disp = _displacement_px(_group_tdfs)
                for tid in group.track_ids:
                    _log_drop(dropped_tracks, flicker_stats_by_tid, tid, drop_reason, group,
                              displacement_px=_group_disp, bpm=None)
                continue
            row = _metrics_curl(
                group, per_track_filtered, all_clean_subtracks,
                traj_by_track, skel_all, fps, frame_col,
                total_frames, long_threshold_s, head_angle_prominence, condition, plate,
            )
            if row is not None:
                lcv, sol, spd = _shape_metrics(group.track_ids, traj, timeseries_df, blob_feats)
                row["length_cv"] = round(lcv, 4) if np.isfinite(lcv) else None
                row["solidity_median"] = round(sol, 4) if np.isfinite(sol) else None
                row["speed_median_abs"] = round(spd, 2) if np.isfinite(spd) else None
                row["worm_index"] = next_worm_idx
                next_worm_idx += 1
                rows.append(row)
        else:  # collision — multi-worm expansion
            if not all_clean_subtracks:
                dropped_collision_too_short += 1
                _group_tdfs = [traj_by_track[tid] for tid in group.track_ids if tid in traj_by_track]
                _group_disp = _displacement_px(_group_tdfs)
                for tid in group.track_ids:
                    _log_drop(dropped_tracks, flicker_stats_by_tid, tid, "collision_too_short", group,
                              displacement_px=_group_disp, bpm=None)
                continue
            N_obs = _max_concurrent(all_clean_subtracks, frame_col)
            N = min(max(N_obs, 1), COLLISION_WORM_COUNT_CAP)
            selected_sts = _select_collision_subtracks(all_clean_subtracks, frame_col, N)
            group_rows: list[dict] = []
            for st in selected_sts:
                if len(st) / fps < MIN_OBSERVATION_TIME_SECONDS:
                    dropped_collision_too_short += 1
                    _log_drop(dropped_tracks, flicker_stats_by_tid,
                               _st_tierpsy_id(st, group), "collision_too_short", group,
                               displacement_px=_displacement_px([st]), bpm=None)
                    continue
                row = _metrics_one_collision_subtrack(
                    st, group, skel_all, fps,
                    total_frames, long_threshold_s, head_angle_prominence, condition, plate,
                )
                if row is not None:
                    st_tids = (list(st["worm_index_joined"].unique())
                               if "worm_index_joined" in st.columns else [])
                    lcv, sol, spd = _shape_metrics(st_tids, traj, timeseries_df, blob_feats)
                    row["length_cv"] = round(lcv, 4) if np.isfinite(lcv) else None
                    row["solidity_median"] = round(sol, 4) if np.isfinite(sol) else None
                    row["speed_median_abs"] = round(spd, 2) if np.isfinite(spd) else None
                    group_rows.append(row)
            if not group_rows:
                continue
            if len(group_rows) > 1:
                multi_worm_clusters += 1
            n_ext = len(group_rows)
            collision_worms_per_cluster[n_ext] = collision_worms_per_cluster.get(n_ext, 0) + 1
            for row in group_rows:
                row["worm_index"] = next_worm_idx
                next_worm_idx += 1
            rows.extend(group_rows)

    # ---- Debris filter (applied after multi-collision expansion) ----
    keep_rows: list[dict] = []
    for r in rows:
        # Rule 1: stationary debris (curl_count guard removed)
        rule1 = (r["displacement_px"] < DEBRIS_DISPLACEMENT_PIXELS
                 and r["bpm"] < DEBRIS_BPM_THRESHOLD)
        # Rule 2: flickery debris — all three shape metrics must be finite and pass threshold
        lcv = r.get("length_cv")
        sol = r.get("solidity_median")
        spd = r.get("speed_median_abs")
        rule2 = (
            lcv is not None and lcv == lcv and lcv > DEBRIS_LENGTH_CV_MIN
            and sol is not None and sol == sol and sol > DEBRIS_SOLIDITY_MIN
            and spd is not None and spd == spd and spd < DEBRIS_SPEED_MAX
        )
        if rule1 or rule2:
            tid = r.get("repr_tierpsy_id", -1)
            fl = flicker_stats_by_tid.get(tid, {})
            dropped_tracks.append({
                "tierpsy_id": tid,
                "reason": "debris",
                "debris_rule": "rule1" if rule1 else "rule2",
                "longest_clean_duration_s": round(r["duration_s"], 3),
                "total_flicker_frames": fl.get("total_flicker_frames", 0),
                "n_fragments_in_group": r.get("fragment_count", 1),
                "group_id": r.get("group_id", -1),
                "displacement_px": round(r["displacement_px"], 3),
                "bpm": round(r["bpm"], 3),
                "length_cv": lcv,
                "solidity_median": sol,
                "speed_median_abs": spd,
            })
        else:
            keep_rows.append(r)

    rows = keep_rows
    dropped_debris = sum(1 for entry in dropped_tracks if entry["reason"] == "debris")

    # ---- Build analysis log ----
    dropped_total = (
        dropped_curl_too_short + dropped_collision_too_short
        + tracks_dropped_flicker + dropped_debris
    )
    analysis_log = {
        "input_track_count": input_track_count,
        "groups_formed": {
            "total": len(groups),
            "curl": n_curl,
            "collision": n_collision,
        },
        "worms_dropped": {
            "total": dropped_total,
            "by_reason": {
                "curl_too_short": dropped_curl_too_short,
                "collision_too_short": dropped_collision_too_short,
                "flicker_killed_track": tracks_dropped_flicker,
                "debris": dropped_debris,
            },
        },
        "flicker_stats": {
            "total_flicker_frames": total_flicker_frames,
            "tracks_with_any_flicker": tracks_with_any_flicker,
            "tracks_dropped_due_to_flicker": tracks_dropped_flicker,
        },
        "fragment_counts": {
            "distribution": {str(k): v for k, v in sorted(fragment_count_dist.items())}
        },
        "multi_worm_clusters": multi_worm_clusters,
        "collision_worms_per_cluster": {
            str(k): v for k, v in sorted(collision_worms_per_cluster.items())
        },
        "dropped_tracks": dropped_tracks,
    }

    return rows, analysis_log


def _empty_log() -> dict:
    return {
        "input_track_count": 0,
        "groups_formed": {"total": 0, "curl": 0, "collision": 0},
        "worms_dropped": {
            "total": 0,
            "by_reason": {
                "curl_too_short": 0,
                "collision_too_short": 0,
                "flicker_killed_track": 0,
                "debris": 0,
            },
        },
        "flicker_stats": {"total_flicker_frames": 0, "tracks_with_any_flicker": 0, "tracks_dropped_due_to_flicker": 0},
        "fragment_counts": {"distribution": {}},
        "multi_worm_clusters": 0,
        "collision_worms_per_cluster": {},
        "dropped_tracks": [],
    }


def _track_wall_clock(track_df: pd.DataFrame, fps: float, frame_col: str) -> "tuple[float, float]":
    """Return (start_s, end_s) from the first and last frame numbers."""
    frames = track_df[frame_col].values.astype(float)
    return float(frames[0]) / fps, float(frames[-1]) / fps


def _metrics_curl(
    group, per_track_filtered, all_clean_subtracks,
    traj_by_track, skel_all, fps, frame_col,
    total_frames, long_threshold_s, head_angle_prominence, condition, plate,
) -> "dict | None":
    """Compute per-worm metrics for a curl group."""
    # Wall clock span: earliest start of any fragment to latest end of any fragment
    track_times = []
    for tid in group.track_ids:
        track_df = traj_by_track.get(tid)
        if track_df is not None and len(track_df) > 0:
            s, e = _track_wall_clock(track_df, fps, frame_col)
            track_times.append((s, e))
    if not track_times:
        return None

    obs_start_s = min(t[0] for t in track_times)
    obs_end_s   = max(t[1] for t in track_times)
    total_obs_s = max(obs_end_s - obs_start_s, 1e-9)

    total_clean_frames = sum(len(st) for st in all_clean_subtracks)
    total_clean_s = total_clean_frames / fps
    valid_frac = min(total_clean_s / total_obs_s, 1.0)

    # Run bend counter on each clean sub-track independently; sum bends
    bend_count = 0
    all_peak_frames: list[int] = []
    for st in all_clean_subtracks:
        sig = compute_head_angle_signal(st, skel_all, fps, head_angle_prominence)
        if sig is None:
            continue
        bend_count += len(sig["pos_peaks"]) + len(sig["neg_peaks"])
        fns = sig["frame_nums"]
        all_peak_frames.extend(fns[sig["pos_peaks"]].tolist())
        all_peak_frames.extend(fns[sig["neg_peaks"]].tolist())
    bend_count = bend_count / 2.0  # half-bends → bends
    cv = bend_interval_cv(np.array(all_peak_frames, dtype=float), fps)

    duration_min = total_clean_s / 60.0
    bpm = bend_count / duration_min if duration_min > 1e-9 else 0.0

    displacement = _displacement_px(all_clean_subtracks)
    coverage_pct = round(total_clean_frames / total_frames * 100, 1)

    # Representative Tierpsy track: earliest-starting fragment
    repr_id = group.repr_track_id

    return {
        "condition": condition,
        "plate": plate,
        "repr_tierpsy_id": repr_id,
        "frames": total_clean_frames,
        "duration_s": round(total_obs_s, 3),
        "bpm": round(float(bpm), 2),
        "bend_interval_cv": cv,
        "is_long": total_obs_s >= long_threshold_s,
        "coverage_pct": coverage_pct,
        "is_full_track": coverage_pct >= 90.0,
        "fps_used": fps,
        "bend_method": "head_angle_peaks_v2",
        "group_classification": "curl",
        "curl_count": group.curl_count,
        "fragment_count": group.fragment_count,
        "valid_frac": round(valid_frac, 3),
        "displacement_px": round(displacement, 1),
        "group_id": group.virtual_worm_id,
    }


def _metrics_one_collision_subtrack(
    repr_st: pd.DataFrame,
    group,
    skel_all: np.ndarray,
    fps: float,
    total_frames: int,
    long_threshold_s: float,
    head_angle_prominence: float,
    condition: str,
    plate: str,
) -> "dict | None":
    """Compute per-worm metrics for one selected sub-track from a collision group."""
    repr_len = len(repr_st)
    total_obs_s = repr_len / fps

    sig = compute_head_angle_signal(repr_st, skel_all, fps, head_angle_prominence)
    if sig is None:
        return None

    bpm = bends_per_minute(sig, fps)
    peak_frames = np.concatenate([
        sig["frame_nums"][sig["pos_peaks"]],
        sig["frame_nums"][sig["neg_peaks"]],
    ]).astype(float)
    cv = bend_interval_cv(peak_frames, fps)
    displacement = _displacement_px([repr_st])
    coverage_pct = round(repr_len / total_frames * 100, 1)

    repr_tierpsy_id = group.repr_track_id
    if "worm_index_joined" in repr_st.columns:
        ids = repr_st["worm_index_joined"].unique()
        if len(ids) == 1:
            repr_tierpsy_id = int(ids[0])

    return {
        "condition": condition,
        "plate": plate,
        "repr_tierpsy_id": repr_tierpsy_id,
        "frames": repr_len,
        "duration_s": round(total_obs_s, 3),
        "bpm": round(float(bpm), 2),
        "bend_interval_cv": cv,
        "is_long": total_obs_s >= long_threshold_s,
        "coverage_pct": coverage_pct,
        "is_full_track": coverage_pct >= 90.0,
        "fps_used": fps,
        "bend_method": "head_angle_peaks_v2",
        "group_classification": "collision",
        "curl_count": 0,
        "fragment_count": 1,
        "valid_frac": 1.0,
        "displacement_px": round(displacement, 1),
        "group_id": group.virtual_worm_id,
    }


# ---------------------------------------------------------------------------
# Summary aggregation (per-video → one row for the summary sheet)
# ---------------------------------------------------------------------------

def build_summary_row(
    fragment_rows: list[dict],
    condition: str,
    plate: str,
    fps: float,
    video_duration_s: float,
    status: str,
) -> dict:
    long_rows = [r for r in fragment_rows if r["is_long"]]
    long_bpms = [r["bpm"] for r in long_rows]

    curl_long   = sum(1 for r in long_rows if r.get("group_classification") == "curl")
    coll_long   = sum(1 for r in long_rows if r.get("group_classification") == "collision")
    mean_valid_frac = (
        float(np.mean([r["valid_frac"] for r in long_rows])) if long_rows else None
    )
    mean_fragments = (
        float(np.mean([r["fragment_count"] for r in long_rows])) if long_rows else None
    )

    long_cvs = [
        r["bend_interval_cv"] for r in long_rows
        if not np.isnan(r.get("bend_interval_cv", float("nan")))
    ]

    def _finite_medians(key: str) -> "float | None":
        vals = [r[key] for r in long_rows if r.get(key) is not None and r[key] == r[key]]
        return round(float(np.median(vals)), 4) if vals else None

    return {
        "condition": condition,
        "plate": plate,
        "n_fragments_total": len(fragment_rows),
        "n_fragments_long": len(long_bpms),
        "bpm_median_long": round(float(np.median(long_bpms)), 2) if long_bpms else None,
        "bpm_mean_long":   round(float(np.mean(long_bpms)),   2) if long_bpms else None,
        "bpm_std_long":    round(float(np.std(long_bpms)),    2) if long_bpms else None,
        "bpm_min_long":    round(float(np.min(long_bpms)),    2) if long_bpms else None,
        "bpm_max_long":    round(float(np.max(long_bpms)),    2) if long_bpms else None,
        "bend_cv_mean_long":   round(float(np.mean(long_cvs)),   4) if long_cvs else None,
        "bend_cv_median_long": round(float(np.median(long_cvs)), 4) if long_cvs else None,
        "length_cv_median_long":      _finite_medians("length_cv"),
        "solidity_median_long":       _finite_medians("solidity_median"),
        "speed_median_abs_median_long": _finite_medians("speed_median_abs"),
        "n_curl_long": curl_long,
        "n_collision_long": coll_long,
        "mean_valid_frac_long": round(mean_valid_frac, 3) if mean_valid_frac is not None else None,
        "mean_fragment_count_long": round(mean_fragments, 2) if mean_fragments is not None else None,
        "fps_used": fps,
        "duration_video_s": round(video_duration_s, 3),
        "status": status,
        "bend_method": "head_angle_peaks_v2",
    }
