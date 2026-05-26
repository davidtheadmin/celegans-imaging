"""
Crawling per-worm metrics + per-condition aggregation.

Self-contained for the crawling pipeline. Computes one row per worm_index
directly from Tierpsy's timeseries_data table (speed / motion_mode / length /
width) and the trajectories_data table (centroid path geometry). The legacy
custom bend-rate calculation is preserved as one extra column by importing
the shared, read-only helpers from analysis_csv.

Heavy third-party imports (numpy/pandas/h5py) live at runtime call sites so
this module can be imported lazily by crawling.py, mirroring how the other
analysis modules are imported only when an analysis run actually starts.
"""
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Canonical metric columns, in output order. Used for per-condition aggregation.
METRIC_COLS: list[str] = [
    "mean_speed_pxs",
    "mean_forward_speed_pxs",
    "mean_backward_speed_pxs",
    "fraction_forward",
    "fraction_backward",
    "fraction_paused",
    "reversal_count",
    "reversal_rate_per_min",
    "path_length_px",
    "net_displacement_px",
    "tortuosity",
    "mean_length_px",
    "mean_width_midbody_px",
    "track_duration_s",
    # Longest gap-free run of consecutive frame_numbers, in seconds. Tracks
    # stitched across gaps by TRAJ_JOIN have this < track_duration_s.
    "longest_continuous_run_s",
    # Fraction of trajectory frames where Tierpsy extracted a skeleton.
    "skeleton_coverage",
    # Legacy custom bend rate — kept as one additional column, no longer primary.
    "bend_rate_bpm",
]

# Identity columns prepended to every per-worm row.
# worm_index is the post-join track id: featuresN timeseries_data.worm_index is
# identical to the _skeletons.hdf5 trajectories_data.worm_index_joined set
# (verified — no worm_index_blob divergence), so we name the column
# worm_index_joined to match the renders, which key on worm_index_joined.
ID_COLS: list[str] = ["condition", "plate", "video_name", "worm_index_joined"]

# Boolean quality flag appended to every per-worm row (see _passes_filter).
QUALITY_COL: str = "passed_filter"

PER_WORM_COLS: list[str] = ID_COLS + METRIC_COLS + [QUALITY_COL]

# Fraction of the video-wide median |speed| below which a frame counts as paused.
_PAUSED_FRACTION_OF_MEDIAN = 0.10

# ---------------------------------------------------------------------------
# Quality-filter thresholds — tune freely; raw per-worm rows are always kept,
# so changing these and re-aggregating needs no Tierpsy re-run.
# LONGEST_RUN_MIN_S is the fallback minimum track duration; callers may override
# it per run (e.g. from the UI "Min track duration (s)" input) by passing
# min_run_s through compute_crawling_metrics / aggregate_per_condition.
# ---------------------------------------------------------------------------
LONGEST_RUN_MIN_S: float = 60.0
SKELETON_COVERAGE_MIN: float = 0.3


def _passes_filter(longest_continuous_run_s, skeleton_coverage,
                   min_run_s: float | None = None) -> bool:
    """
    Return True if a worm track is high-quality enough to enter the aggregate.

    min_run_s overrides the minimum-duration threshold; when None (or non-finite)
    the module-level LONGEST_RUN_MIN_S fallback is used.
    """
    try:
        run = float(longest_continuous_run_s)
        sc = float(skeleton_coverage)
    except (TypeError, ValueError):
        return False
    try:
        threshold = float(min_run_s)
        if not np.isfinite(threshold):
            threshold = LONGEST_RUN_MIN_S
    except (TypeError, ValueError):
        threshold = LONGEST_RUN_MIN_S
    return bool(
        np.isfinite(run) and run >= threshold
        and np.isfinite(sc) and sc >= SKELETON_COVERAGE_MIN
    )


def _mean_finite(arr: np.ndarray) -> float:
    arr = arr[np.isfinite(arr)]
    return float(np.mean(arr)) if len(arr) else float("nan")


def _longest_run_s(frame_numbers: np.ndarray, fps: float) -> float:
    """Longest run of consecutive integer frame numbers (no gaps), in seconds."""
    fr = frame_numbers[np.isfinite(frame_numbers)]
    if len(fr) == 0 or fps <= 0:
        return float("nan")
    frames = np.unique(fr.astype(np.int64))
    best = cur = 1
    for i in range(1, len(frames)):
        if frames[i] == frames[i - 1] + 1:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 1
    return best / fps


def _read_skeleton_traj_meta(skeletons_path: Path, fps: float) -> dict:
    """
    Read trajectories_data from a *_skeletons.hdf5 file and return, per
    worm_index_joined, a (skeleton_coverage, longest_continuous_run_s) tuple.

    skeleton_coverage      — mean of the has_skeleton flag, a fraction in [0, 1].
    longest_continuous_run_s — longest gap-free frame_number run, in seconds.

    Returns an empty dict (and logs) if the file or its columns are unavailable.
    The has_skeleton column lives here, not in the _featuresN.hdf5 trajectories.
    """
    import pandas as pd

    meta: dict[int, tuple[float, float]] = {}
    try:
        st = pd.read_hdf(str(skeletons_path), key="trajectories_data")
    except Exception:
        log.warning("crawling: could not read trajectories_data from %s",
                    skeletons_path, exc_info=True)
        return meta
    if "worm_index_joined" not in st.columns or "frame_number" not in st.columns:
        log.warning("crawling: %s trajectories_data missing worm_index_joined/frame_number",
                    getattr(skeletons_path, "name", skeletons_path))
        return meta

    has_skel = "has_skeleton" in st.columns
    if not has_skel:
        log.warning("crawling: %s trajectories_data has no has_skeleton column",
                    getattr(skeletons_path, "name", skeletons_path))
    for wi, g in st.groupby("worm_index_joined"):
        coverage = float("nan")
        if has_skel:
            hs = g["has_skeleton"].values.astype(float)
            hs = hs[np.isfinite(hs)]
            if len(hs):
                coverage = float(np.mean(hs))
        run_s = _longest_run_s(g["frame_number"].values.astype(float), fps)
        meta[int(wi)] = (coverage, run_s)
    return meta


def compute_crawling_metrics(
    hdf5_path: Path,
    fps: float,
    condition: str,
    plate: str,
    video_name: str,
    head_angle_prominence: float = 0.30,
    min_run_s: float | None = None,
) -> list[dict]:
    """
    Compute crawling metrics for every worm_index in one _featuresN.hdf5.

    Returns a list of per-worm dicts keyed by PER_WORM_COLS. An empty list is
    returned (and logged) if timeseries_data cannot be read.

    min_run_s sets the minimum-duration threshold for the passed_filter flag;
    when None the module-level LONGEST_RUN_MIN_S fallback is used.
    """
    import h5py
    import pandas as pd

    try:
        ts = pd.read_hdf(str(hdf5_path), key="timeseries_data")
    except Exception as exc:
        log.error("crawling: failed to read timeseries_data from %s: %s", hdf5_path, exc)
        return []
    if ts is None or len(ts) == 0 or "worm_index" not in ts.columns:
        log.warning("crawling: timeseries_data empty or missing worm_index in %s", hdf5_path)
        return []

    # Trajectories (centroid path) + skeletons (legacy bend rate) — best effort.
    try:
        traj = pd.read_hdf(str(hdf5_path), key="trajectories_data")
    except Exception:
        traj = None
    skel_all = None
    try:
        with h5py.File(str(hdf5_path), "r") as fh:
            skel_all = fh["coordinates"]["skeletons"][:]
    except Exception:
        skel_all = None

    has_motion_mode = "motion_mode" in ts.columns
    ts_frame_col = "timestamp" if "timestamp" in ts.columns else "frame_number"

    # Video-wide paused threshold = 10% of median |speed| across all worms.
    if "speed" in ts.columns:
        all_speeds = np.abs(ts["speed"].values.astype(float))
        all_speeds = all_speeds[np.isfinite(all_speeds)]
        median_abs_speed = float(np.median(all_speeds)) if len(all_speeds) else 0.0
    else:
        median_abs_speed = 0.0
    paused_threshold = _PAUSED_FRACTION_OF_MEDIAN * median_abs_speed

    # Pre-index trajectory centroids by worm (worm_index == worm_index_joined).
    traj_by_worm: dict[int, "pd.DataFrame"] = {}
    if traj is not None and "worm_index_joined" in traj.columns:
        traj_frame_col = "timestamp_raw" if "timestamp_raw" in traj.columns else "frame_number"
        for wi, g in traj.groupby("worm_index_joined"):
            traj_by_worm[int(wi)] = g.sort_values(traj_frame_col).reset_index(drop=True)

    # skeleton_coverage + longest_continuous_run come from the *_skeletons.hdf5
    # trajectories_data (the has_skeleton flag is not present in *_featuresN.hdf5).
    _feat_path = Path(hdf5_path)
    skeletons_path = _feat_path.with_name(_feat_path.name.replace("_featuresN", "_skeletons"))
    skel_meta = _read_skeleton_traj_meta(skeletons_path, fps)

    # Lazy import of shared, read-only bend-rate helpers.
    try:
        from analysis.analysis_csv import compute_head_angle_signal, bends_per_minute
    except Exception:
        compute_head_angle_signal = None
        bends_per_minute = None

    rows: list[dict] = []
    for wi, g in ts.groupby("worm_index"):
        wi = int(wi)
        g = g.sort_values(ts_frame_col)

        speed = (g["speed"].values.astype(float)
                 if "speed" in g.columns else np.array([], dtype=float))
        speed = speed[np.isfinite(speed)]
        n = len(speed)
        abs_speed = np.abs(speed)
        fwd = speed[speed > 0]
        bwd = speed[speed < 0]

        # --- reversal_count: forward→backward transitions ---
        if has_motion_mode:
            mm = g["motion_mode"].values.astype(float)
            mm = mm[np.isfinite(mm)]
            seq = mm[mm != 0]  # collapse paused frames; keep forward(1)/backward(-1)
            reversal_count = (int(np.sum((seq[:-1] == 1) & (seq[1:] == -1)))
                              if len(seq) > 1 else 0)
        else:
            # Fallback: positive→negative sign changes in speed.
            signs = np.sign(speed)
            signs = signs[signs != 0]
            reversal_count = (int(np.sum((signs[:-1] == 1) & (signs[1:] == -1)))
                              if len(signs) > 1 else 0)

        # --- track duration ---
        frames = g[ts_frame_col].values.astype(float)
        frames = frames[np.isfinite(frames)]
        if len(frames) >= 1 and fps > 0:
            track_duration_s = (float(frames.max()) - float(frames.min()) + 1.0) / fps
        else:
            track_duration_s = float("nan")
        dur_min = (track_duration_s / 60.0
                   if np.isfinite(track_duration_s) else 0.0)
        reversal_rate_per_min = (reversal_count / dur_min) if dur_min > 1e-9 else 0.0

        # --- centroid path geometry ---
        path_length_px = float("nan")
        net_displacement_px = float("nan")
        tortuosity = float("nan")
        tdf = traj_by_worm.get(wi)
        if tdf is not None and "coord_x" in tdf.columns and "coord_y" in tdf.columns:
            cx = tdf["coord_x"].values.astype(float)
            cy = tdf["coord_y"].values.astype(float)
            m = np.isfinite(cx) & np.isfinite(cy)
            cx, cy = cx[m], cy[m]
            if len(cx) >= 2:
                dx = np.diff(cx)
                dy = np.diff(cy)
                path_length_px = float(np.sum(np.sqrt(dx * dx + dy * dy)))
                net_displacement_px = float(np.hypot(cx[-1] - cx[0], cy[-1] - cy[0]))
                tortuosity = path_length_px / max(net_displacement_px, 1.0)
            elif len(cx) == 1:
                path_length_px = 0.0
                net_displacement_px = 0.0
                tortuosity = 0.0

        # --- skeleton coverage + longest continuous run (from *_skeletons.hdf5) ---
        skeleton_coverage, longest_continuous_run_s = skel_meta.get(
            wi, (float("nan"), float("nan"))
        )
        # Fallback for the run length only if the skeletons file lacked this worm.
        if not np.isfinite(longest_continuous_run_s) and tdf is not None:
            fcol = ("frame_number" if "frame_number" in tdf.columns
                    else "timestamp_raw" if "timestamp_raw" in tdf.columns else None)
            if fcol is not None:
                longest_continuous_run_s = _longest_run_s(
                    tdf[fcol].values.astype(float), fps
                )

        # --- legacy custom bend rate ---
        bend_rate_bpm = float("nan")
        if (tdf is not None and skel_all is not None
                and compute_head_angle_signal is not None and bends_per_minute is not None):
            try:
                sig = compute_head_angle_signal(tdf, skel_all, fps, head_angle_prominence)
                if sig is not None:
                    bend_rate_bpm = float(bends_per_minute(sig, fps))
            except Exception:
                log.debug("crawling: bend-rate failed for worm %d", wi, exc_info=True)

        rows.append({
            "condition": condition,
            "plate": plate,
            "video_name": video_name,
            "worm_index_joined": wi,
            "mean_speed_pxs": _mean_finite(abs_speed),
            "mean_forward_speed_pxs": (float(np.mean(fwd)) if len(fwd) else 0.0),
            "mean_backward_speed_pxs": (float(np.mean(np.abs(bwd))) if len(bwd) else 0.0),
            "fraction_forward": (float(np.mean(speed > 0)) if n else float("nan")),
            "fraction_backward": (float(np.mean(speed < 0)) if n else float("nan")),
            "fraction_paused": (float(np.mean(abs_speed < paused_threshold))
                                if n else float("nan")),
            "reversal_count": reversal_count,
            "reversal_rate_per_min": reversal_rate_per_min,
            "path_length_px": path_length_px,
            "net_displacement_px": net_displacement_px,
            "tortuosity": tortuosity,
            "mean_length_px": (_mean_finite(g["length"].values.astype(float))
                               if "length" in g.columns else float("nan")),
            "mean_width_midbody_px": (_mean_finite(g["width_midbody"].values.astype(float))
                                      if "width_midbody" in g.columns else float("nan")),
            "track_duration_s": track_duration_s,
            "longest_continuous_run_s": longest_continuous_run_s,
            "skeleton_coverage": skeleton_coverage,
            "bend_rate_bpm": bend_rate_bpm,
            "passed_filter": _passes_filter(
                longest_continuous_run_s, skeleton_coverage, min_run_s
            ),
        })

    return rows


def aggregate_per_condition(per_worm_rows: list[dict],
                            min_run_s: float | None = None) -> list[dict]:
    """
    Aggregate per-worm rows into one row per condition.

    Low-quality tracks (see _passes_filter / LONGEST_RUN_MIN_S /
    SKELETON_COVERAGE_MIN) are dropped before computing metric aggregates.
    The filter is recomputed here from the raw longest_continuous_run_s /
    skeleton_coverage columns, so thresholds can be re-tuned on a saved
    per_worm table without re-running Tierpsy. min_run_s overrides the
    minimum-duration threshold (must match the value used to build the
    per_worm passed_filter column); None falls back to LONGEST_RUN_MIN_S.

    Each condition row reports n_worms_total (before filtering),
    n_worms_kept (after filtering), and mean / median / std of every metric
    computed only on the kept worms.
    """
    import pandas as pd

    if not per_worm_rows:
        return []

    df = pd.DataFrame(per_worm_rows)
    out: list[dict] = []
    for cond in sorted(df["condition"].astype(str).unique()):
        cdf = df[df["condition"].astype(str) == cond]
        passed_mask = cdf.apply(
            lambda r: _passes_filter(
                r.get("longest_continuous_run_s"), r.get("skeleton_coverage"),
                min_run_s,
            ),
            axis=1,
        )
        kept = cdf[passed_mask.values]
        agg: dict = {
            "condition": cond,
            "n_worms_total": int(len(cdf)),
            "n_worms_kept": int(len(kept)),
        }
        for col in METRIC_COLS:
            if col in kept.columns:
                vals = pd.to_numeric(kept[col], errors="coerce").values.astype(float)
                vals = vals[np.isfinite(vals)]
            else:
                vals = np.array([], dtype=float)
            if len(vals):
                agg[f"{col}_mean"] = float(np.mean(vals))
                agg[f"{col}_median"] = float(np.median(vals))
                agg[f"{col}_std"] = float(np.std(vals))
            else:
                agg[f"{col}_mean"] = None
                agg[f"{col}_median"] = None
                agg[f"{col}_std"] = None
        out.append(agg)
    return out
