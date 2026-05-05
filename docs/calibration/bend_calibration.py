"""
Compare bend-counting methods against manual counts.
Tests 7 methods to find the one that best matches the lab's manual protocol
("head goes to one side of the body and back").

Run from launcher/ with the launcher venv active.
"""
import numpy as np
import pandas as pd
import tables
from scipy.signal import find_peaks

# ---------------------------------------------------------------------------
# Manual counts from the technician (bends per 30 s)
# ---------------------------------------------------------------------------
MANUAL = [
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\fast\_wormscan_cache\20260505T084249_video\Results\20260505T084249_video_featuresN.hdf5",  1,  44, "WT"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\fast\_wormscan_cache\20260505T084249_video\Results\20260505T084249_video_featuresN.hdf5",  3,  46, "WT"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\fast\_wormscan_cache\20260505T084249_video\Results\20260505T084249_video_featuresN.hdf5",  4,  46, "WT"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\fast\_wormscan_cache\20260505T084249_video\Results\20260505T084249_video_featuresN.hdf5", 10,  31, "WT"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\slow\_wormscan_cache\20260505T090942_video\Results\20260505T090942_video_featuresN.hdf5",  1,  13, "slow"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\slow\_wormscan_cache\20260505T090942_video\Results\20260505T090942_video_featuresN.hdf5",  2,   2, "slow"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\slow\_wormscan_cache\20260505T090942_video\Results\20260505T090942_video_featuresN.hdf5",  3,   8, "slow"),
    (r"C:\Users\Isabe\Documents\WormScan\experiments\calibration test\slow\_wormscan_cache\20260505T090942_video\Results\20260505T090942_video_featuresN.hdf5",  4,  10, "slow"),
]
FPS = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def detrend(sig, fps, smooth_s=0.3, baseline_s=2.0):
    """Smooth + subtract slow rolling baseline."""
    smooth_win = max(3, int(fps * smooth_s) | 1)
    smoothed = pd.Series(sig).rolling(smooth_win, center=True, min_periods=1).mean().values
    baseline_win = max(5, int(fps * baseline_s) | 1)
    baseline = pd.Series(smoothed).rolling(baseline_win, center=True, min_periods=1).mean().values
    return smoothed - baseline


def normalise_to_30s(count, n_valid_samples, fps):
    if n_valid_samples == 0:
        return np.nan
    duration_s = n_valid_samples / fps
    return count * (30.0 / duration_s)


# ---------------------------------------------------------------------------
# Bend-counting methods
# ---------------------------------------------------------------------------
def method_curvature_peaks(curvature, fps, prominence):
    """Original method: detrended midbody curvature, peaks above prominence."""
    sig = curvature[~np.isnan(curvature)]
    if len(sig) < 10:
        return np.nan
    detrended = detrend(sig, fps)
    pos, _ = find_peaks(detrended, prominence=prominence)
    neg, _ = find_peaks(-detrended, prominence=prominence)
    half_bends = len(pos) + len(neg)
    return normalise_to_30s(half_bends / 2, len(sig), fps)


def method_eigen_rotation(eigen1, eigen2, fps):
    """Full rotations of (eigen_1, eigen_2) in 2D phase space."""
    mask = ~(np.isnan(eigen1) | np.isnan(eigen2))
    e1, e2 = eigen1[mask], eigen2[mask]
    if len(e1) < 10:
        return np.nan
    angles = np.unwrap(np.arctan2(e2, e1))
    rotations = abs(angles[-1] - angles[0]) / (2 * np.pi)
    return normalise_to_30s(rotations, len(e1), fps)


def method_eigen1_peaks(eigen1, fps, prominence):
    """Peaks in eigen_projection_1 (C-shape mode dominant in swimming)."""
    sig = eigen1[~np.isnan(eigen1)]
    if len(sig) < 10:
        return np.nan
    detrended = detrend(sig, fps)
    pos, _ = find_peaks(detrended, prominence=prominence)
    neg, _ = find_peaks(-detrended, prominence=prominence)
    half_bends = len(pos) + len(neg)
    return normalise_to_30s(half_bends / 2, len(sig), fps)


def method_eigen_path_length(eigen1, eigen2, fps):
    """Total path length traced in (eigen_1, eigen_2) space.
    Returns 'effective bend count' = path length / typical-bend-circumference.
    For swimming worms, one bend ~= traversing ~2*pi*amplitude in eigen space.
    """
    mask = ~(np.isnan(eigen1) | np.isnan(eigen2))
    e1, e2 = eigen1[mask], eigen2[mask]
    if len(e1) < 10:
        return np.nan
    de1 = np.diff(e1)
    de2 = np.diff(e2)
    path = np.sum(np.hypot(de1, de2))
    # Estimate "one bend" as 2*pi*RMS amplitude — empirical scaling
    amplitude = np.hypot(e1, e2).std()
    if amplitude < 1e-6:
        return 0.0
    bends = path / (2 * np.pi * amplitude)
    return normalise_to_30s(bends, len(e1), fps)


def method_head_angle_peaks(skeletons, frame_indices, fps, prominence):
    """The technician's definition: head swings to one side of the body, then back.
    
    Compute the angle between the head segment direction (skeleton[0:5])
    and the body axis (skeleton[20:30]) per frame. Count peak crossings.
    """
    n_frames = len(skeletons)
    if n_frames < 10:
        return np.nan
    
    angles = np.full(n_frames, np.nan)
    for i in range(n_frames):
        skel = skeletons[i]
        if not np.isfinite(skel).all():
            continue
        # Head direction: vector from skeleton[5] to skeleton[0]
        head_vec = skel[0] - skel[5]
        # Body axis: vector from skeleton[30] to skeleton[20] (midbody direction)
        body_vec = skel[20] - skel[30]
        # Signed angle between them, using cross-product sign
        angle = np.arctan2(
            head_vec[0] * body_vec[1] - head_vec[1] * body_vec[0],  # cross
            head_vec[0] * body_vec[0] + head_vec[1] * body_vec[1],  # dot
        )
        angles[i] = angle
    
    sig = angles[~np.isnan(angles)]
    if len(sig) < 10:
        return np.nan
    detrended = detrend(sig, fps)
    pos, _ = find_peaks(detrended, prominence=prominence)
    neg, _ = find_peaks(-detrended, prominence=prominence)
    half_bends = len(pos) + len(neg)
    return normalise_to_30s(half_bends / 2, len(sig), fps)


# ---------------------------------------------------------------------------
# Per-worm extraction
# ---------------------------------------------------------------------------
def get_worm_data(features_path, worm_index):
    """Return curvature, eigen_1, eigen_2, skeletons (per-frame) for a worm."""
    f = tables.open_file(features_path, "r")
    ts = pd.DataFrame.from_records(f.root.timeseries_data.read())
    traj = pd.DataFrame.from_records(f.root.trajectories_data.read())
    skel_all = f.root.coordinates.skeletons.read()
    f.close()
    
    worm_ts = ts[ts["worm_index"] == worm_index].sort_values("timestamp").reset_index(drop=True)
    worm_traj = traj[traj["worm_index_joined"] == worm_index].sort_values("timestamp_raw").reset_index(drop=True)
    
    if len(worm_ts) == 0 or len(worm_traj) == 0:
        return None
    
    # Map skeleton rows: trajectories_data has a `skeleton_id` column pointing into the skeleton array
    skel_ids = worm_traj["skeleton_id"].values
    valid = (skel_ids >= 0) & (skel_ids < len(skel_all))
    skeletons = np.full((len(worm_traj), 49, 2), np.nan)
    skeletons[valid] = skel_all[skel_ids[valid].astype(int)]
    
    return {
        "curvature": worm_ts["curvature_midbody"].values,
        "eigen1": worm_ts["eigen_projection_1"].values,
        "eigen2": worm_ts["eigen_projection_2"].values,
        "skeletons": skeletons,
        "frame_idx": worm_ts["timestamp"].values,
    }


# ---------------------------------------------------------------------------
# Run comparison
# ---------------------------------------------------------------------------
def main():
    rows = []
    for path, wi, manual, label in MANUAL:
        d = get_worm_data(path, wi)
        if d is None:
            print(f"WARNING: no data for worm {wi}")
            continue
        
        row = {"worm": f"{label}-{wi}", "manual": manual}
        
        # Original method (current production)
        for p in [0.005, 0.010, 0.020]:
            row[f"curv_p{p:.3f}"] = method_curvature_peaks(d["curvature"], FPS, p)
        
        # Eigen rotation (last winner)
        row["eigen_rot"] = method_eigen_rotation(d["eigen1"], d["eigen2"], FPS)
        
        # Eigen-1 peaks (C-shape mode dominant in swimming)
        for p in [0.05, 0.10, 0.20]:
            row[f"eig1_p{p:.2f}"] = method_eigen1_peaks(d["eigen1"], FPS, p)
        
        # Eigen path length (catches one-sided wobblers)
        row["eig_path"] = method_eigen_path_length(d["eigen1"], d["eigen2"], FPS)
        
        # Head-angle peaks (the technician's definition!)
        for p in [0.10, 0.20, 0.30, 0.40]:
            row[f"head_p{p:.2f}"] = method_head_angle_peaks(d["skeletons"], d["frame_idx"], FPS, p)
        
        rows.append(row)
    
    df = pd.DataFrame(rows)
    print("\n=== Per-worm counts (bends per 30s) ===\n")
    print(df.to_string(index=False, float_format="%.1f"))
    
    print("\n=== Mean absolute error vs manual count ===\n")
    method_cols = [c for c in df.columns if c not in ("worm", "manual")]
    errors = {}
    for col in method_cols:
        diffs = (df[col] - df["manual"]).abs()
        mae = diffs.mean()
        bias = (df[col] - df["manual"]).mean()
        errors[col] = (mae, bias)
        print(f"  {col:>15s}  MAE={mae:5.1f}  bias={bias:+5.1f}")
    
    print("\nLowest MAE wins. Bias positive = overcounts, negative = undercounts.")
    
    sorted_methods = sorted(errors.items(), key=lambda kv: kv[1][0])
    print(f"\n=== Top 3 methods ===")
    for name, (mae, bias) in sorted_methods[:3]:
        print(f"  {name:>15s}  MAE={mae:5.1f}  bias={bias:+5.1f}")


if __name__ == "__main__":
    main()