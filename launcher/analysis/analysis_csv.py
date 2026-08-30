"""
Motility analysis: bend counting + fragment-grouping pipeline.

Pipeline config
---------------
All thresholds are starting defaults; tune after first run on validation videos.
"""
import hashlib
import json
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
# The shortest PIECE of uninterrupted skeleton that is worth measuring at all.
# Bends are counted inside a piece and never across a gap, so a piece is the
# measurement unit; below about three seconds it holds too few swings to say
# anything (3 s at a healthy ~90 bpm is about four bends).
#
# This is NOT the gate on the worm. That is the number set in the launcher,
# and it applies to the SUM of a worm's surviving pieces: how much good signal
# must exist in total before the animal counts. Two thresholds, two jobs —
# "how short may one piece be" and "how much must they add up to".
#
# It used to be one hard-coded 10.0 applied to the total, while the launcher's
# box set something else entirely (threshold_s -> is_long, applied much later),
# so a user who asked for 5 s still silently lost everything under 10. On
# `601 0J plate 01` that cost two moving animals with 8.3 s and 5.1 s of clean
# signal, shown as grey dots with no way to tell why.
MIN_PIECE_S: float = 3.0
COLLISION_WORM_COUNT_CAP: int = 3           # max worms extracted from one collision cluster
DEBRIS_DISPLACEMENT_PIXELS: float = 8.0    # debris filter: max displacement
DEBRIS_BPM_THRESHOLD: float = 5.0          # debris filter: max BPM
DEBRIS_LENGTH_CV_MIN: float = 0.10         # debris filter: flickery if length_cv exceeds this
DEBRIS_SOLIDITY_MIN: float = 0.6           # debris filter: blob-shaped if solidity_median exceeds this
DEBRIS_SPEED_MAX: float = 10.0             # debris filter: high speed = real worm (safety gate)
# Plate-edge debris. Flat-field correction lifted the periphery enough that
# the plate edge segments as an object, and it lands between the first two
# rules: rule 1 needs displacement under 8 px and the edge measured 11.9, and
# rule 2 needs length_cv over 0.10 to call something flickery and the edge is
# rock steady at 0.057.
#
# MEASURED, on the 27 Aug 260521_Motility run, 144 worms, 36 videos. Solidity
# separates cleanly and nothing else does: real swimmers run 0.26-0.51
# (p50 0.35, p95 0.44), then a gap, then 0.619 and 0.793 — the two plate-edge
# objects, one of which is `601 0J plate 02` worm 0, the one David identified
# by eye. A swimming worm is a thin curve and fills little of its convex hull;
# the edge fragment is a compact blob.
#
# Rule 3 — a NON-MOVING, EDGE-SHAPED object. Solidity was the wrong measure:
# David identified `903 20J plate 03` worm 0 (solidity 0.619, motionless) as a
# real worm, so compactness cannot carry this. Thickness can.
#
# MEASURED from the Tierpsy features of all 142 worms of the 27 Aug run:
#
#                        minor_axis        major/minor
#   plate edge (601 0J p02 w0)   1.87           46.84
#   thinnest real worm           6.54            ...
#   real worm population    6.54 - 35.8     2.21 - 9.63
#
# The edge is a two-pixel line. A worm — swimming, curled or paralysed — has a
# body 6.5 to 36 px thick. Both gaps are enormous: the thresholds below sit
# 1.6x clear of the nearest real worm on each axis and 2-3x clear of the edge.
# Both are required, and so is stillness, because a moving object is an animal
# whatever its shape.
# Thickness was dropped from this rule. It was fitted to one plate edge that
# happened to be 1.87 px across, and a 4.0 px ceiling then missed the next one:
# `601 0J plate 02` worm 4 on the 28 Aug run is 454 px long at aspect 58.7 —
# unmistakably a hair or an edge — but 7.6 px across, so it sailed through.
# Elongation alone separates it and separates it hugely: over all 182 worms the
# aspect ratio runs to 8.6 at its highest and then jumps to 58.7. A threshold
# at 15 sits 1.7x above every real animal and 3.9x below the artefact.
EDGE_ASPECT_MIN: float = 15.0                # rule 3: major/minor, worms top out at 8.6

# Rule 4 — a SLOW object with a worm's AREA but nothing like a worm's
# LENGTH. David identified worm 7 of `601 10J plate 02` as debris; measured
# under the adopted parameters it is the clearest separation in the file:
#
#                       area    length   solidity  compactness  skel cov
#   the debris           646     43.7      0.832      0.550       0.14
#   the five real worms 648-836 106-120  0.39-0.50  0.13-0.16   0.50-1.00
#
# Area is useless here — 646 against 648. Length is a 2.4x gap with nothing in
# between. The rule is RELATIVE to the video's own median worm length, not an
# absolute pixel count, because every absolute threshold in this file has had
# to be refitted each time the binarisation moved. A ratio does not.
# The debris sits at 0.38 of the median; the real worms at 0.92-1.03.
#
# IT DELIBERATELY DOES NOT TEST BEND RATE. It did, and it therefore fired on
# nothing: `601 20J plate 01` worm 4 reads 25.6 bpm and `903 0J plate 01` worm
# 4 reads 16.9, because a flickering skeleton on a stationary blob fakes a bend
# rate. That is the same way the plate edge got past rule 1. Motion is what
# debris can imitate; shape is not. Speed stays, because it is what separates a
# small real worm (swimming at 14-32 px/s) from a blob (1-2 px/s) — over the
# 170 worms of the 28 Aug run, length+speed drops 6 and all 6 are blobs, while
# length alone would also take five animals swimming at 59-129 bpm.
DEBRIS_SHORT_LENGTH_FRAC: float = 0.55       # rule 4: x the video's median worm length
# Rule 4's second half. Debris is short AND it does not behave like an animal,
# but there are two ways not to: a blob sits still (speed 1-2 px/s), and a hair
# DRIFTS — `903 0J plate 02` worm 1 is 0.41 of its plate's median length and
# travels at 14.8 px/s, above the speed gate, while bending 1.5 times a minute.
# So either failure qualifies.
#
# 40 bpm is chosen with room on both sides: the fake bend rates flicker
# produces on debris measured 1.5 to 28.6, and the genuinely small worms on
# these plates swim at 60 to 129. Across all 182 worms the pair of terms takes
# exactly one object, the hair.
DEBRIS_DRIFT_BPM_MAX: float = 40.0           # rule 4: below this it is not swimming


# ---------------------------------------------------------------------------
# What the run cache is allowed to reuse. See run_cache.settings_digest.
# ---------------------------------------------------------------------------
def tuning_constants() -> dict:
    """Every module-level tuning constant above, by name.

    Collected from the module rather than listed by hand, so a threshold added
    to the block above enters the reuse digest by existing. The scan takes
    public UPPER_CASE ints and floats; anything else here that is not a
    threshold would over-invalidate the cache, which is the safe direction.
    """
    return {k: float(v) for k, v in sorted(globals().items())
            if k.isupper() and not k.startswith("_")
            and isinstance(v, (int, float)) and not isinstance(v, bool)}


def reuse_post_settings(threshold_s: float) -> dict:
    """The post-Tierpsy half of motility's run-cache digest — the ONE spelling.

    Mirrors crawling_metrics.reuse_post_settings. Until 27 Aug motility hashed
    `{"threshold_s": …}` and nothing else, so every threshold in the block
    above could be changed and a re-run would hand back the old rows and report
    the change as having done nothing. That is exactly how the crawling
    skeleton floor came to be measured as a no-op — see
    claude/crawling-rerun-reuse-trap-2026-08-27.md.

    KNOWN GAP, deliberate. Motility's per-worm columns are the union of the
    keys the rows actually carry (`motility.py`, `_mot_cols`), not a static
    list, so there is no column set to hash the way crawling hashes
    PER_WORM_COLS. Adding a metric to a row therefore still does not
    invalidate the cache by itself — it goes missing from a reused run's CSV
    rather than arriving empty. Bump `row_schema` by hand when the row fields
    change, or give motility a static column list and hash that.
    """
    return {
        "threshold_s": float(threshold_s),
        "row_schema": 8,   # 8: rule 3 on elongation alone, rule 4 also
                           #    catches a drifting hair
        "tuning": hashlib.sha256(
            json.dumps(tuning_constants(), sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12],
    }



# ---------------------------------------------------------------------------
# Bend counter (head_angle_peaks_v2). Black box: takes a clean angle
# time-series, returns peak counts.
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
    prominence: float,
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


def bend_interval_cv_pieces(intervals) -> float:
    """CV of inter-peak intervals that were each measured INSIDE one piece.

    The old code pooled peak TIMES from every clean sub-track into one array
    and diffed it, so the dropout between two pieces became an inter-bend
    interval: a worm with 5 s of signal, a 10 s loss and 5 s more contributed
    one ~10 s "interval" to a distribution whose real values are near 0.7 s.
    The mean rose, the SD rose much further, and the metric whose whole job is
    to say whether a rhythm is regular reported an artefact of tracking.

    Bends are never counted across a gap; neither are the gaps between them.
    Two intervals minimum, matching the three-peak minimum it replaces.
    """
    iv = np.asarray(list(intervals), dtype=float)
    iv = iv[np.isfinite(iv)]
    if len(iv) < 2:
        return float("nan")
    mean_iv = float(np.mean(iv))
    if mean_iv < 1e-9:
        return float("nan")
    return float(np.std(iv) / mean_iv)


def bend_amplitude(peak_heights_rad: np.ndarray) -> "tuple[float, float]":
    """
    (mean head-swing amplitude in DEGREES, its coefficient of variation).

    `peak_heights_rad` is the detrended head angle at each detected peak. The
    sign is dropped: a positive and a negative peak of the same size are the
    same excursion in opposite directions, and both are amplitude.

    Degrees because this is read off a figure; the CV is scale-free either way.
    NaN below three peaks, matching bend_interval_cv — with two you can compute
    a spread but you cannot believe it.
    """
    h = np.asarray(peak_heights_rad, dtype=float)
    h = np.abs(h[np.isfinite(h)])
    if len(h) < 3:
        return float("nan"), float("nan")
    mean_rad = float(np.mean(h))
    if mean_rad < 1e-9:
        return float("nan"), float("nan")
    return float(np.degrees(mean_rad)), float(np.std(h) / mean_rad)


# ---------------------------------------------------------------------------
# Displacement helper
# ---------------------------------------------------------------------------

def _displacement_px(track_dfs: List[pd.DataFrame]) -> float:
    """
    Max pairwise centroid distance across all provided track DataFrames.
    Uses a random subsample (≤ 300 points) for speed.

    The subsample is drawn from a per-call local Generator rather than the
    process-global np.random state. This preserves the original random-subsample
    behaviour but is thread-safe under the parallel pipeline — the global RNG is
    shared mutable state that concurrent worker threads would otherwise race,
    adding cross-thread run-to-run variation on top of the inherent randomness.
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
        idx = np.random.default_rng().choice(len(all_x), 300, replace=False)
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
    Compute (length_cv, length_median, solidity_median, speed_median_abs,
    minor_axis_median, aspect_median) for a worm group. NaN for any metric with
    insufficient data.

    length_median is the reference rule 4 compares against: the plate edge is
    far longer than any worm on the plate, and that is the most worm-shaped
    thing about a worm that an arc cannot fake.
    """
    length_cv = float("nan")
    length_median = float("nan")
    minor_axis_median = float("nan")
    aspect_median = float("nan")
    speed_median_abs = float("nan")
    solidity_median = float("nan")

    if timeseries_df is not None and "worm_index" in timeseries_df.columns:
        ts_sub = timeseries_df[timeseries_df["worm_index"].isin(track_ids)]
        if "length" in ts_sub.columns:
            lengths = ts_sub["length"].values.astype(float)
            lengths = lengths[np.isfinite(lengths)]
            if len(lengths) >= 10 and np.mean(lengths) > 0:
                length_cv = float(np.std(lengths) / np.mean(lengths))
                length_median = float(np.median(lengths))
        if "minor_axis" in ts_sub.columns and "major_axis" in ts_sub.columns:
            mi = ts_sub["minor_axis"].values.astype(float)
            ma = ts_sub["major_axis"].values.astype(float)
            ok = np.isfinite(mi) & np.isfinite(ma) & (mi > 1e-6)
            if ok.sum() >= 10:
                minor_axis_median = float(np.median(mi[ok]))
                aspect_median = float(np.median(ma[ok] / mi[ok]))
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

    return (length_cv, length_median, solidity_median, speed_median_abs,
            minor_axis_median, aspect_median)


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
    min_observation_s: "float | None" = None,
    distance_threshold_px: float = DISTANCE_THRESHOLD_PIXELS,
    time_gap_threshold_s: float = TIME_GAP_THRESHOLD_SECONDS,
) -> "tuple[list[dict], dict]":
    """
    Run the full fragment-grouping + flicker-filter pipeline on one _featuresN.hdf5.

    Returns (per_worm_rows, analysis_log).

    ``min_observation_s`` is the least clean observation time a worm may have
    and still be measured. None means follow ``long_threshold_s`` — the number
    the user set — clamped up to MIN_OBSERVATION_FLOOR_S.

    Per-worm rows contain all previous columns plus:
        curl_count, fragment_count, valid_frac, group_classification, repr_tierpsy_id

    distance_threshold_px / time_gap_threshold_s tune fragment grouping; the
    defaults equal the module constants so motility (which omits them) is
    unchanged. Crawling passes a wider distance to rejoin crossing handoffs.
    """
    from analysis.fragment_grouping import group_fragments
    from analysis.flicker_filter import filter_track
    import h5py

    # Two thresholds, two jobs — see MIN_PIECE_S. `min_piece_s` discards a
    # fragment too short to measure; `min_total_s` is the user's number and
    # gates the worm on what its surviving fragments add up to.
    min_piece_s = MIN_PIECE_S
    min_total_s = (float(long_threshold_s) if min_observation_s is None
                   else float(min_observation_s))
    min_total_s = max(min_piece_s, min_total_s)

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
    groups = group_fragments(traj, fps, distance_threshold_px, time_gap_threshold_s)

    n_curl = sum(1 for g in groups if g.classification == "curl")
    n_collision = sum(1 for g in groups if g.classification == "collision")

    # ---- Steps 3–5: per-group processing ----
    rows: list[dict] = []

    # Logging accumulators
    total_flicker_frames = 0
    tracks_with_any_flicker = 0
    tracks_dropped_flicker = 0
    dropped_curl_too_short = 0
    pieces_dropped_short = 0
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

        # A fragment shorter than min_piece_s carries no usable rhythm, so it
        # is not a measurement and must not pad the total either.
        _short = [st for st in all_clean_subtracks if len(st) / fps < min_piece_s]
        all_clean_subtracks = [st for st in all_clean_subtracks
                               if len(st) / fps >= min_piece_s]
        pieces_dropped_short += len(_short)
        total_clean_frames = sum(len(st) for st in all_clean_subtracks)
        total_clean_s = total_clean_frames / fps

        # ---- Steps 4+5: drop short worms and compute metrics ----
        if group.classification == "curl":
            if total_clean_s < min_total_s:
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
                lcv, lmed, sol, spd, mnr, asp = _shape_metrics(group.track_ids, traj, timeseries_df, blob_feats)
                row["minor_axis_median"] = round(mnr, 3) if np.isfinite(mnr) else None
                row["aspect_median"] = round(asp, 2) if np.isfinite(asp) else None
                row["length_median"] = round(lmed, 2) if np.isfinite(lmed) else None
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
                if len(st) / fps < min_total_s:
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
                    lcv, lmed, sol, spd, mnr, asp = _shape_metrics(st_tids, traj, timeseries_df, blob_feats)
                    row["minor_axis_median"] = round(mnr, 3) if np.isfinite(mnr) else None
                    row["aspect_median"] = round(asp, 2) if np.isfinite(asp) else None
                    row["length_median"] = round(lmed, 2) if np.isfinite(lmed) else None
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
    # Rule 4's reference: this video's own median worm length. With too few
    # objects the median is not a population, it is one of the suspects, so the
    # rule is skipped and the skip is recorded — the same guard the crawling
    # skeleton floor uses. Note the median is taken over ALL rows including the
    # debris; with one short object among six the median is unmoved, and a
    # video that is mostly debris has no reference worth having anyway.
    _ref_lengths = [r["length_median"] for r in rows
                    if r.get("length_median") is not None]
    length_ref = (float(np.median(_ref_lengths))
                  if len(_ref_lengths) >= 5 else None)
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
        # Rule 3: a non-moving, edge-shaped object — see EDGE_ASPECT_MIN.
        # Thin AND elongated AND going nowhere AND not bending. Every term has
        # to hold; any one of them alone describes some real worm in the set.
        lmed = r.get("length_median")
        mnr, asp = r.get("minor_axis_median"), r.get("aspect_median")
        rule3 = (asp is not None and asp == asp and asp > EDGE_ASPECT_MIN
                 and spd is not None and spd == spd and spd < DEBRIS_SPEED_MAX
                 and r["bpm"] < DEBRIS_BPM_THRESHOLD)
        # Rule 4: a worm's area, nothing like a worm's length, and going
        # nowhere. length_median is NaN for an object with under ten
        # skeletonised frames, and the rule then cannot fire — a real worm that
        # barely skeletonises is kept, which is the safe direction.
        rule4 = (length_ref is not None
                 and lmed is not None and lmed == lmed
                 and lmed < length_ref * DEBRIS_SHORT_LENGTH_FRAC
                 and ((spd is not None and spd == spd and spd < DEBRIS_SPEED_MAX)
                      or r["bpm"] < DEBRIS_DRIFT_BPM_MAX))
        if rule1 or rule2 or rule3 or rule4:
            tid = r.get("repr_tierpsy_id", -1)
            fl = flicker_stats_by_tid.get(tid, {})
            dropped_tracks.append({
                "tierpsy_id": tid,
                "reason": "debris",
                "debris_rule": ("rule1" if rule1 else "rule2" if rule2
                                else "rule3" if rule3 else "rule4"),
                "minor_axis_median": mnr,
                "aspect_median": asp,
                "longest_clean_duration_s": round(r["duration_s"], 3),
                "total_flicker_frames": fl.get("total_flicker_frames", 0),
                "n_fragments_in_group": r.get("fragment_count", 1),
                "group_id": r.get("group_id", -1),
                "displacement_px": round(r["displacement_px"], 3),
                "bpm": round(r["bpm"], 3),
                "length_cv": lcv,
                "solidity_median": sol,
                "speed_median_abs": spd,
                "length_median": lmed,
            })
        else:
            keep_rows.append(r)

    rows = keep_rows
    dropped_debris = sum(1 for entry in dropped_tracks if entry["reason"] == "debris")
    debris_by_rule: dict[str, int] = {}
    for _e in dropped_tracks:
        if _e["reason"] == "debris":
            _k = str(_e.get("debris_rule", "unknown"))
            debris_by_rule[_k] = debris_by_rule.get(_k, 0) + 1

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
            "debris_by_rule": debris_by_rule,
        },
        "min_piece_s": round(min_piece_s, 3),
        "min_total_s": round(min_total_s, 3),
        "pieces_dropped_too_short": pieces_dropped_short,
        "plate_edge_filter": {
            "note": ("rules 3 and 4 drop slow objects that are not "
                     "worm-shaped — see EDGE_ASPECT_MIN and "
                     "DEBRIS_SHORT_LENGTH_FRAC in analysis_csv.py"),
            "median_worm_length_px": (round(length_ref, 2)
                                      if length_ref is not None else None),
            "short_object_cutoff_px": (round(length_ref * DEBRIS_SHORT_LENGTH_FRAC, 2)
                                       if length_ref is not None else None),
            "reference_worms": len(_ref_lengths),
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


# ---------------------------------------------------------------------------
# Shared engine boundary (Brief 1, Step 2)
# ---------------------------------------------------------------------------
# read_fragments IS the grouping + flicker + BPM engine: it returns grouped
# worm rows (each with member_tierpsy_ids) and has no Excel / plot / render side
# effects. The crawling pipeline imports it under this name to obtain the same
# grouped identities motility uses, then layers its own kinematics on top.
# Calling it from crawling does not change motility output in any way.
produce_grouped_worm_rows = read_fragments


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
            "debris_by_rule": {},
        },
        "min_piece_s": None,
        "min_total_s": None,
        "pieces_dropped_too_short": 0,
        "plate_edge_filter": {
            "note": "rules 3 and 4 drop slow non-worm-shaped objects",
            "median_worm_length_px": None,
            "short_object_cutoff_px": None,
            "reference_worms": 0,
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
    all_intervals: list[float] = []
    all_peak_heights: list[float] = []
    for st in all_clean_subtracks:
        sig = compute_head_angle_signal(st, skel_all, fps, head_angle_prominence)
        if sig is None:
            continue
        bend_count += len(sig["pos_peaks"]) + len(sig["neg_peaks"])
        fns = sig["frame_nums"]
        # Intervals WITHIN this piece only — never across the gap to the next.
        _pk = np.sort(np.concatenate([
            fns[sig["pos_peaks"]], fns[sig["neg_peaks"]]]).astype(float))
        if len(_pk) >= 2:
            all_intervals.extend((np.diff(_pk) / fps).tolist())
        _det = sig["detrended"]
        all_peak_heights.extend(_det[sig["pos_peaks"]].tolist())
        all_peak_heights.extend(_det[sig["neg_peaks"]].tolist())
    bend_count = bend_count / 2.0  # half-bends → bends
    cv = bend_interval_cv_pieces(all_intervals)
    amp_deg, amp_cv = bend_amplitude(np.array(all_peak_heights, dtype=float))

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
        "member_tierpsy_ids": sorted(group.track_ids),
        "frames": total_clean_frames,
        "duration_s": round(total_obs_s, 3),
        "bpm": round(float(bpm), 2),
        "bend_interval_cv": cv,
        "amplitude_deg": round(amp_deg, 2) if amp_deg == amp_deg else None,
        "amplitude_cv": round(amp_cv, 4) if amp_cv == amp_cv else None,
        "is_long": total_obs_s >= long_threshold_s,
        "coverage_pct": coverage_pct,
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
    amp_deg, amp_cv = bend_amplitude(np.concatenate([
        sig["detrended"][sig["pos_peaks"]],
        sig["detrended"][sig["neg_peaks"]],
    ]).astype(float))
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
        "member_tierpsy_ids": [repr_tierpsy_id],
        "frames": repr_len,
        "duration_s": round(total_obs_s, 3),
        "bpm": round(float(bpm), 2),
        "bend_interval_cv": cv,
        "amplitude_deg": round(amp_deg, 2) if amp_deg == amp_deg else None,
        "amplitude_cv": round(amp_cv, 4) if amp_cv == amp_cv else None,
        "is_long": total_obs_s >= long_threshold_s,
        "coverage_pct": coverage_pct,
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
