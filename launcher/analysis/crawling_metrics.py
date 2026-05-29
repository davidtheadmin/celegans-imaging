"""
Crawling per-worm metrics + per-condition aggregation.

Brief 1: crawling now shares motility's grouping + flicker + BPM engine
(analysis_csv.produce_grouped_worm_rows / read_fragments). The engine returns
one row per GROUPED worm — fragments stitched into a single identity, with a
member_tierpsy_ids list — carrying bpm, bend_interval_cv and is_long. This
module layers crawling's kinematic metrics (speed, reversals, path geometry,
tortuosity, net displacement, continuous-run length, skeleton coverage) on top
of each grouped worm's COMBINED track, then applies crawling's own quality gate
(min track duration + skeleton coverage).

Heavy third-party imports (numpy/pandas/h5py) live at runtime call sites so this
module can be imported lazily by crawling.py, mirroring the other analysis
modules.
"""
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Identity columns prepended to every per-worm row. worm_index is the grouped
# (engine-assigned) identity; repr_tierpsy_id is the representative Tierpsy
# track; member_tierpsy_ids lists every Tierpsy worm_index_joined stitched into
# this grouped worm (";"-joined for the spreadsheet).
ID_COLS: list[str] = [
    "condition", "plate", "video_name",
    "worm_index", "repr_tierpsy_id", "member_tierpsy_ids", "group_classification",
]

# Engine-derived columns (identical mechanism to motility).
ENGINE_COLS: list[str] = ["bpm", "bend_interval_cv", "is_long"]

# Canonical kinematic metric columns, in output order. Used for per-condition
# aggregation alongside the engine BPM columns.
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
    # Longest gap-free run of consecutive frame_numbers, in seconds (union of
    # the grouped worm's member frames).
    "longest_continuous_run_s",
    # Fraction of trajectory frames where Tierpsy extracted a skeleton.
    "skeleton_coverage",
]

# Columns aggregated per condition (engine BPM metrics + kinematics).
AGG_COLS: list[str] = ["bpm", "bend_interval_cv"] + METRIC_COLS

# Boolean quality flag appended to every per-worm row (see _passes_filter).
QUALITY_COL: str = "passed_filter"

PER_WORM_COLS: list[str] = ID_COLS + ENGINE_COLS + METRIC_COLS + [QUALITY_COL]

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


def _read_skeleton_traj_raw(skeletons_path: Path) -> dict:
    """
    Read trajectories_data from a *_skeletons.hdf5 file and return, per
    worm_index_joined, a (frames, has_skeleton) tuple of aligned float arrays.

    has_skeleton is None when the column is absent. Returns an empty dict (and
    logs) if the file or its columns are unavailable. The has_skeleton column
    lives here, not in the _featuresN.hdf5 trajectories.
    """
    import pandas as pd

    raw: dict[int, tuple[np.ndarray, "np.ndarray | None"]] = {}
    try:
        st = pd.read_hdf(str(skeletons_path), key="trajectories_data")
    except Exception:
        log.warning("crawling: could not read trajectories_data from %s",
                    skeletons_path, exc_info=True)
        return raw
    if "worm_index_joined" not in st.columns or "frame_number" not in st.columns:
        log.warning("crawling: %s trajectories_data missing worm_index_joined/frame_number",
                    getattr(skeletons_path, "name", skeletons_path))
        return raw

    has_skel = "has_skeleton" in st.columns
    if not has_skel:
        log.warning("crawling: %s trajectories_data has no has_skeleton column",
                    getattr(skeletons_path, "name", skeletons_path))
    for wi, g in st.groupby("worm_index_joined"):
        frames = g["frame_number"].values.astype(float)
        hs = (g["has_skeleton"].values.astype(float) if has_skel else None)
        raw[int(wi)] = (frames, hs)
    return raw


def _aggregate_skel_meta(skel_raw: dict, member_ids: list[int],
                         fps: float) -> tuple[float, float]:
    """
    Aggregate skeleton coverage + longest continuous run over a grouped worm's
    member tracks (union of their frames).

    skeleton_coverage      — mean of has_skeleton across all member frames.
    longest_continuous_run_s — longest gap-free frame run over the union, in s.
    """
    all_frames: list[np.ndarray] = []
    all_hs: list[np.ndarray] = []
    for mid in member_ids:
        entry = skel_raw.get(int(mid))
        if entry is None:
            continue
        frames, hs = entry
        all_frames.append(frames)
        if hs is not None:
            all_hs.append(hs)
    if not all_frames:
        return float("nan"), float("nan")
    frames_union = np.concatenate(all_frames)
    coverage = float("nan")
    if all_hs:
        hs_all = np.concatenate(all_hs)
        hs_all = hs_all[np.isfinite(hs_all)]
        if len(hs_all):
            coverage = float(np.mean(hs_all))
    run_s = _longest_run_s(frames_union, fps)
    return coverage, run_s


def _combined_path_geometry(member_ids: list[int],
                            traj_by_worm: dict, fps: float) -> tuple[float, float, float]:
    """
    Path length, net displacement and tortuosity for a grouped worm's combined
    centroid track.

    The members are concatenated into one continuous track over sampled frames
    and sorted by frame; path length sums only the steps between frame-adjacent
    samples, so gaps (within or between fragments) contribute no path and there
    is no jump-interpolation across them. Net displacement spans the first and
    last sampled centroid overall.
    """
    fxy: list[tuple[float, float, float]] = []
    for mid in member_ids:
        tdf = traj_by_worm.get(int(mid))
        if tdf is None or "coord_x" not in tdf.columns or "coord_y" not in tdf.columns:
            continue
        fcol = ("frame_number" if "frame_number" in tdf.columns
                else "timestamp_raw" if "timestamp_raw" in tdf.columns else None)
        if fcol is None:
            continue
        fr = tdf[fcol].values.astype(float)
        cx = tdf["coord_x"].values.astype(float)
        cy = tdf["coord_y"].values.astype(float)
        m = np.isfinite(fr) & np.isfinite(cx) & np.isfinite(cy)
        for f, x, y in zip(fr[m], cx[m], cy[m]):
            fxy.append((float(f), float(x), float(y)))
    if len(fxy) < 2:
        if len(fxy) == 1:
            return 0.0, 0.0, 0.0
        return float("nan"), float("nan"), float("nan")

    fxy.sort(key=lambda t: t[0])
    f = np.array([t[0] for t in fxy])
    x = np.array([t[1] for t in fxy])
    y = np.array([t[2] for t in fxy])

    adjacent = np.diff(f) == 1.0  # count only frame-adjacent sampled steps
    seg = np.sqrt(np.diff(x) ** 2 + np.diff(y) ** 2)
    path_length_px = float(np.sum(seg[adjacent]))
    net_displacement_px = float(np.hypot(x[-1] - x[0], y[-1] - y[0]))
    tortuosity = path_length_px / max(net_displacement_px, 1.0)
    return path_length_px, net_displacement_px, tortuosity


def compute_crawling_metrics(
    hdf5_path: Path,
    fps: float,
    condition: str,
    plate: str,
    video_name: str,
    head_angle_prominence: float = 0.30,
    long_threshold_s: float = 5.0,
    min_run_s: float | None = None,
) -> list[dict]:
    """
    Compute crawling metrics for every GROUPED worm in one _featuresN.hdf5.

    Each row pairs the shared engine's grouped identity (worm_index,
    repr_tierpsy_id, member_tierpsy_ids), engine BPM (bpm, bend_interval_cv) and
    is_long flag with crawling's kinematic metrics, computed on the grouped
    worm's combined track. A "reversal_frames" key (list of frame numbers where a
    forward->backward reversal fired) is added to every row for the renderer; it
    is intentionally absent from PER_WORM_COLS (not a spreadsheet column).

    Returns an empty list (and logs) if the engine produced no rows or
    timeseries_data cannot be read. long_threshold_s drives is_long exactly as in
    motility; min_run_s sets the passed_filter minimum-duration threshold (None
    falls back to LONGEST_RUN_MIN_S).
    """
    import h5py
    import pandas as pd

    from analysis.analysis_csv import produce_grouped_worm_rows

    # ---- Shared grouping + flicker + BPM engine (motility-identical) ----
    try:
        engine_rows, _engine_log = produce_grouped_worm_rows(
            hdf5_path, fps, condition, plate,
            long_threshold_s=long_threshold_s,
            head_angle_prominence=head_angle_prominence,
        )
    except Exception:
        log.error("crawling: grouping engine failed for %s", hdf5_path, exc_info=True)
        return []
    if not engine_rows:
        log.warning("crawling: engine produced no grouped worms for %s", hdf5_path)
        return []

    # ---- timeseries_data (speed / motion_mode / length / width) ----
    try:
        ts = pd.read_hdf(str(hdf5_path), key="timeseries_data")
    except Exception as exc:
        log.error("crawling: failed to read timeseries_data from %s: %s", hdf5_path, exc)
        return []
    if ts is None or len(ts) == 0 or "worm_index" not in ts.columns:
        log.warning("crawling: timeseries_data empty or missing worm_index in %s", hdf5_path)
        return []

    # ---- trajectories_data (centroid path geometry) — best effort ----
    try:
        traj = pd.read_hdf(str(hdf5_path), key="trajectories_data")
    except Exception:
        traj = None

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

    # Pre-index timeseries + trajectory rows by Tierpsy worm_index
    # (worm_index == trajectories worm_index_joined == engine member id).
    ts_by_worm: dict[int, "pd.DataFrame"] = {
        int(wi): g.sort_values(ts_frame_col) for wi, g in ts.groupby("worm_index")
    }
    traj_by_worm: dict[int, "pd.DataFrame"] = {}
    if traj is not None and "worm_index_joined" in traj.columns:
        traj_frame_col = "timestamp_raw" if "timestamp_raw" in traj.columns else "frame_number"
        for wi, g in traj.groupby("worm_index_joined"):
            traj_by_worm[int(wi)] = g.sort_values(traj_frame_col).reset_index(drop=True)

    # skeleton_coverage + longest_continuous_run come from the *_skeletons.hdf5
    # trajectories_data (the has_skeleton flag is not in *_featuresN.hdf5).
    _feat_path = Path(hdf5_path)
    skeletons_path = _feat_path.with_name(_feat_path.name.replace("_featuresN", "_skeletons"))
    skel_raw = _read_skeleton_traj_raw(skeletons_path)

    rows: list[dict] = []
    for er in engine_rows:
        member_ids = [int(t) for t in (er.get("member_tierpsy_ids") or [])]
        if not member_ids:
            rid = er.get("repr_tierpsy_id")
            member_ids = [int(rid)] if rid is not None else []

        # --- combined timeseries track over all members ---
        member_ts = [ts_by_worm[m] for m in member_ids if m in ts_by_worm]
        if member_ts:
            g = pd.concat(member_ts).sort_values(ts_frame_col)
        else:
            g = ts.iloc[0:0]

        speed = (g["speed"].values.astype(float)
                 if "speed" in g.columns else np.array([], dtype=float))
        speed = speed[np.isfinite(speed)]
        n = len(speed)
        abs_speed = np.abs(speed)
        fwd = speed[speed > 0]
        bwd = speed[speed < 0]

        # --- reversal_count + reversal_frames: forward->backward transitions ---
        # Keep frame numbers aligned through the paused-frame collapse so the
        # frame at which each reversal fires can be recovered for the renderer.
        reversal_count = 0
        reversal_frames: list[int] = []
        frames_ts = (g[ts_frame_col].values.astype(float)
                     if ts_frame_col in g.columns else np.array([], dtype=float))
        if has_motion_mode and "motion_mode" in g.columns:
            mm = g["motion_mode"].values.astype(float)
            valid = np.isfinite(mm) & np.isfinite(frames_ts)
            mm_v = mm[valid]
            fr_v = frames_ts[valid]
            nz = mm_v != 0  # collapse paused frames; keep forward(1)/backward(-1)
            seq = mm_v[nz]
            seq_fr = fr_v[nz]
        else:
            # Fallback: positive->negative sign changes in speed.
            sp = (g["speed"].values.astype(float)
                  if "speed" in g.columns else np.array([], dtype=float))
            valid = np.isfinite(sp) & np.isfinite(frames_ts)
            signs = np.sign(sp[valid])
            fr_v = frames_ts[valid]
            nz = signs != 0
            seq = signs[nz]
            seq_fr = fr_v[nz]
        if len(seq) > 1:
            rev_mask = (seq[:-1] == 1) & (seq[1:] == -1)
            reversal_count = int(np.sum(rev_mask))
            # Frame at which backward motion begins = the reversal frame.
            reversal_frames = sorted(
                {int(f) for f in seq_fr[1:][rev_mask] if np.isfinite(f)}
            )

        # --- track duration (combined) ---
        ft = frames_ts[np.isfinite(frames_ts)]
        if len(ft) >= 1 and fps > 0:
            track_duration_s = (float(ft.max()) - float(ft.min()) + 1.0) / fps
        else:
            track_duration_s = float("nan")
        dur_min = (track_duration_s / 60.0
                   if np.isfinite(track_duration_s) else 0.0)
        reversal_rate_per_min = (reversal_count / dur_min) if dur_min > 1e-9 else 0.0

        # --- centroid path geometry (combined, gap-aware) ---
        path_length_px, net_displacement_px, tortuosity = _combined_path_geometry(
            member_ids, traj_by_worm, fps
        )

        # --- skeleton coverage + longest continuous run (union of members) ---
        skeleton_coverage, longest_continuous_run_s = _aggregate_skel_meta(
            skel_raw, member_ids, fps
        )
        if not np.isfinite(longest_continuous_run_s) and len(ft):
            # Fallback to the combined timeseries frames if skeletons lacked them.
            longest_continuous_run_s = _longest_run_s(ft, fps)

        rows.append({
            "condition": condition,
            "plate": plate,
            "video_name": video_name,
            "worm_index": er.get("worm_index"),
            "repr_tierpsy_id": er.get("repr_tierpsy_id"),
            "member_tierpsy_ids": ";".join(str(m) for m in member_ids),
            "group_classification": er.get("group_classification"),
            "bpm": er.get("bpm"),
            "bend_interval_cv": er.get("bend_interval_cv"),
            "is_long": er.get("is_long"),
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
            "passed_filter": _passes_filter(
                longest_continuous_run_s, skeleton_coverage, min_run_s
            ),
            # Renderer-only (not a spreadsheet column).
            "reversal_frames": reversal_frames,
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
        for col in AGG_COLS:
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
