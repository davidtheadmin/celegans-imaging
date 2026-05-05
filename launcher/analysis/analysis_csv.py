import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

log = logging.getLogger(__name__)


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

    # Drop rows where the frame number itself is non-finite
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


def read_fragments(
    hdf5_path: Path,
    fps: float,
    condition: str,
    plate: str,
    long_threshold_s: float = 5.0,
    head_angle_prominence: float = 0.30,
) -> list[dict]:
    """Read trajectories_data + skeletons from a _featuresN.hdf5, return per-fragment rows."""
    import h5py

    try:
        ts = pd.read_hdf(str(hdf5_path), key="timeseries_data")
        traj = pd.read_hdf(str(hdf5_path), key="trajectories_data")
    except Exception as exc:
        log.error("Failed to read %s: %s", hdf5_path, exc)
        return []

    try:
        with h5py.File(str(hdf5_path), "r") as fh:
            skel_all = fh["coordinates"]["skeletons"][:]
    except Exception as exc:
        log.error("Failed to read skeletons from %s: %s", hdf5_path, exc)
        return []

    ts_col = "timestamp" if "timestamp" in ts.columns else "frame_number"
    total_frames = max(int(ts[ts_col].max()) + 1, 1)

    # Group timeseries by worm for duration/coverage (unchanged from before)
    ts_by_worm = {wi: g for wi, g in ts.groupby("worm_index")}

    frame_col = "timestamp_raw" if "timestamp_raw" in traj.columns else "frame_number"

    rows: list[dict] = []
    for worm_index, worm_traj in traj.groupby("worm_index_joined"):
        worm_traj = worm_traj.sort_values(frame_col).reset_index(drop=True)

        signal = compute_head_angle_signal(worm_traj, skel_all, fps, head_angle_prominence)
        if signal is None:
            continue

        bpm = bends_per_minute(signal, fps)

        ts_grp = ts_by_worm.get(worm_index)
        frames = len(ts_grp) if ts_grp is not None else signal["n_valid"]
        duration_s = frames / fps
        coverage_pct = round(frames / total_frames * 100, 1)

        rows.append(
            {
                "condition": condition,
                "plate": plate,
                "worm_index": int(worm_index),
                "frames": frames,
                "duration_s": round(duration_s, 3),
                "bpm": round(bpm, 2),
                "is_long": duration_s >= long_threshold_s,
                "coverage_pct": coverage_pct,
                "is_full_track": coverage_pct >= 90.0,
                "fps_used": fps,
                "bend_method": "head_angle_peaks_v1",
            }
        )
    return rows


def build_summary_row(
    fragment_rows: list[dict],
    condition: str,
    plate: str,
    fps: float,
    video_duration_s: float,
    status: str,
) -> dict:
    long_bpms = [r["bpm"] for r in fragment_rows if r["is_long"]]
    return {
        "condition": condition,
        "plate": plate,
        "n_fragments_total": len(fragment_rows),
        "n_fragments_long": len(long_bpms),
        "bpm_median_long": round(float(np.median(long_bpms)), 2) if long_bpms else None,
        "bpm_mean_long": round(float(np.mean(long_bpms)), 2) if long_bpms else None,
        "bpm_std_long": round(float(np.std(long_bpms)), 2) if long_bpms else None,
        "bpm_min_long": round(float(np.min(long_bpms)), 2) if long_bpms else None,
        "bpm_max_long": round(float(np.max(long_bpms)), 2) if long_bpms else None,
        "fps_used": fps,
        "duration_video_s": round(video_duration_s, 3),
        "status": status,
        "bend_method": "head_angle_peaks_v1",
    }
