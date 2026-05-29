"""
Crawling per-worm metrics + per-condition aggregation.

Grouping uses crawling's own position-based fragment linker
(crawling_fragment_grouping.link_fragments), NOT the motility engine: crawling
worms are slow crawlers Tierpsy repeatedly loses, and the linker rejoins their
shattered fragments by nearest-neighbour end->start matching with an ambiguity
refusal (see that module). Each linked group becomes one GROUPED worm
(worm_index = group id, member_tierpsy_ids = members, repr = first by start).

This module then layers crawling's kinematic metrics (speed, reversals, path
geometry, tortuosity, net displacement, continuous-run length, skeleton coverage)
plus head-angle bpm / bend_interval_cv on each grouped worm's COMBINED member
track, and applies crawling's own quality gate (min track duration + skeleton
coverage). bpm / bend_interval_cv are computed here from the concatenated member
skeletons (compute_head_angle_signal / bend_interval_cv), so observed-frame-only
gap exclusion happens naturally; is_long is derived from track_duration_s.

Metrics never impute across gaps: speed fractions average observed frames only,
path length sums frame-adjacent steps only, bpm divides by valid-skeleton-frame
seconds, and longest_continuous_run_s is the longest single member fragment.

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

# Head-angle / duration-derived columns. Kept under the historical ENGINE_COLS
# name so the spreadsheet schema is unchanged; bpm and bend_interval_cv are now
# computed from the concatenated member skeletons in this module, and is_long is
# derived from track_duration_s (no motility engine involved).
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

# Max frame separation between two consecutive non-paused frames for a
# forward->backward transition to count as a reversal. A larger separation means
# a tracking gap (or pause) sits between them, so the "reversal" spans a gap and
# is discarded — we only count reversals observed across (near-)adjacent frames.
REVERSAL_MAX_GAP_FRAMES: int = 2

# ---------------------------------------------------------------------------
# Quality-filter thresholds — tune freely; raw per-worm rows are always kept,
# so changing these and re-aggregating needs no Tierpsy re-run.
# LONGEST_RUN_MIN_S is the fallback minimum track duration; callers may override
# it per run (e.g. from the UI "Min track duration (s)" input) by passing
# min_run_s through compute_crawling_metrics / aggregate_per_condition.
# ---------------------------------------------------------------------------
LONGEST_RUN_MIN_S: float = 30.0
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


def _linker_log(n_fragments: int, n_groups: int, ambig_skips: int) -> dict:
    """
    Build an analysis_log compatible with the shared-engine sidecar that
    crawling.py expects.

    The position linker neither classifies groups (curl/collision) nor drops
    fragments — the downstream quality gate does the dropping — so those fields
    are zero/empty. ambiguity_skips is a linker-specific diagnostic: how often the
    ambiguity rule refused a link (a genuine crossing left deliberately broken).
    """
    return {
        "input_track_count": int(n_fragments),
        "groups_formed": {"total": int(n_groups), "curl": 0, "collision": 0},
        "worms_dropped": {"total": 0, "by_reason": {}},
        "ambiguity_skips": int(ambig_skips),
    }


def _skel_coverage_and_run(
    skel_by_worm: dict, member_ids: list[int], fps: float, has_skel_col: bool,
) -> tuple[float, float]:
    """
    Skeleton coverage + longest continuous run over a grouped worm's members.

    skeleton_coverage        — mean of has_skeleton across all member frames
                               (NaN if no has_skeleton column / no members).
    longest_continuous_run_s — longest gap-free run of consecutive frames within a
                               SINGLE member fragment (NOT bridged across members),
                               over skeletonised frames, in seconds. The linker may
                               chain many short fragments into one identity, so the
                               run length is taken per-member to preserve the meaning
                               of the >=Ns gate: the worm was tracked UNBROKEN for
                               that long.
    """
    all_hs: list[np.ndarray] = []
    longest_run_s = 0.0
    have_any = False
    for mid in member_ids:
        sub = skel_by_worm.get(int(mid))
        if sub is None:
            continue
        have_any = True
        frames = sub["frame_number"].values.astype(float)
        if has_skel_col:
            hs = sub["has_skeleton"].values.astype(float)
            all_hs.append(hs)
            good = frames[np.isfinite(hs) & (hs >= 0.5)]  # skeletonised frames only
        else:
            good = frames  # no has_skeleton column: treat every tracked frame as good
        run_s = _longest_run_s(good, fps)
        if np.isfinite(run_s):
            longest_run_s = max(longest_run_s, run_s)
    if not have_any:
        return float("nan"), float("nan")
    coverage = float("nan")
    if all_hs:
        hs_all = np.concatenate(all_hs)
        hs_all = hs_all[np.isfinite(hs_all)]
        if len(hs_all):
            coverage = float(np.mean(hs_all))
    return coverage, longest_run_s


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
    engine_log_out: dict | None = None,
) -> list[dict]:
    """
    Compute crawling metrics for every GROUPED worm in one _featuresN.hdf5.

    Worms are grouped by the position-based fragment linker
    (crawling_fragment_grouping.link_fragments) over the *_skeletons.hdf5
    trajectories_data (where has_skeleton lives). Each linked group becomes one
    row: worm_index = the group id, member_tierpsy_ids = its members, and
    repr_tierpsy_id = the first member by start frame. Crawling's kinematics
    (speed fractions, reversals, path geometry, tortuosity, net displacement) plus
    head-angle bpm / bend_interval_cv are computed on the grouped worm's combined
    member track; is_long is derived from track_duration_s. A "reversal_frames"
    key (frame numbers where a forward->backward reversal fired) is added for the
    renderer; it is intentionally absent from PER_WORM_COLS.

    Returns an empty list (and logs) if the linker produced no groups or
    timeseries_data cannot be read. long_threshold_s drives is_long; min_run_s
    sets the passed_filter minimum-duration threshold (None falls back to
    LONGEST_RUN_MIN_S). When engine_log_out is provided, a linker analysis_log
    (input_track_count, groups_formed, worms_dropped, ambiguity_skips) is copied
    into it so callers can surface grouping diagnostics.
    """
    import h5py
    import pandas as pd

    from analysis.crawling_fragment_grouping import link_fragments
    from analysis.analysis_csv import compute_head_angle_signal, bend_interval_cv

    _feat_path = Path(hdf5_path)
    skeletons_path = _feat_path.with_name(_feat_path.name.replace("_featuresN", "_skeletons"))

    # ---- Grouping source: *_skeletons.hdf5 trajectories_data ----
    try:
        skel_traj = pd.read_hdf(str(skeletons_path), key="trajectories_data")
    except Exception:
        log.error("crawling: could not read trajectories_data from %s",
                  skeletons_path, exc_info=True)
        return []
    if (skel_traj is None or len(skel_traj) == 0
            or "worm_index_joined" not in skel_traj.columns
            or "frame_number" not in skel_traj.columns):
        log.warning("crawling: %s trajectories_data empty or missing columns", skeletons_path)
        return []

    groups, ambig_skips = link_fragments(skel_traj, fps)
    n_fragments = int(skel_traj["worm_index_joined"].nunique())
    if not groups:
        log.warning("crawling: linker produced no groups for %s", skeletons_path)
        if engine_log_out is not None:
            engine_log_out.update(_linker_log(n_fragments, 0, ambig_skips))
        return []
    if engine_log_out is not None:
        engine_log_out.update(_linker_log(n_fragments, len(groups), ambig_skips))

    has_skel_col = "has_skeleton" in skel_traj.columns
    if not has_skel_col:
        log.warning("crawling: %s trajectories_data has no has_skeleton column", skeletons_path)
    skel_by_worm: dict[int, "pd.DataFrame"] = {
        int(wid): sub.sort_values("frame_number")
        for wid, sub in skel_traj.groupby("worm_index_joined")
    }

    # ---- timeseries_data (speed / motion_mode / length / width) ----
    try:
        ts = pd.read_hdf(str(hdf5_path), key="timeseries_data")
    except Exception as exc:
        log.error("crawling: failed to read timeseries_data from %s: %s", hdf5_path, exc)
        return []
    if ts is None or len(ts) == 0 or "worm_index" not in ts.columns:
        log.warning("crawling: timeseries_data empty or missing worm_index in %s", hdf5_path)
        return []

    # ---- featuresN trajectories_data (centroid path + skeleton_id) — best effort ----
    try:
        feat_traj = pd.read_hdf(str(hdf5_path), key="trajectories_data")
    except Exception:
        feat_traj = None

    # ---- featuresN skeleton coordinate array (head-angle bpm) — best effort ----
    try:
        with h5py.File(str(hdf5_path), "r") as fh:
            skel_all = fh["coordinates"]["skeletons"][:]
    except Exception:
        log.warning("crawling: could not read coordinates/skeletons from %s; bpm will be 0",
                    hdf5_path, exc_info=True)
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

    # Pre-index timeseries + featuresN trajectory rows by Tierpsy worm_index
    # (worm_index == trajectories worm_index_joined == linker member id).
    ts_by_worm: dict[int, "pd.DataFrame"] = {
        int(wi): g.sort_values(ts_frame_col) for wi, g in ts.groupby("worm_index")
    }
    traj_by_worm: dict[int, "pd.DataFrame"] = {}        # path geometry (coords)
    feat_traj_by_worm: dict[int, "pd.DataFrame"] = {}   # head-angle (needs skeleton_id)
    if feat_traj is not None and "worm_index_joined" in feat_traj.columns:
        traj_frame_col = "timestamp_raw" if "timestamp_raw" in feat_traj.columns else "frame_number"
        for wi, g in feat_traj.groupby("worm_index_joined"):
            gs = g.sort_values(traj_frame_col).reset_index(drop=True)
            traj_by_worm[int(wi)] = gs
            feat_traj_by_worm[int(wi)] = gs

    # Order groups by their earliest fragment start so worm_index / row order is
    # stable run-to-run (the union-find group id is otherwise arbitrary). Each
    # group's members are ordered by start frame; member[0] is the representative.
    def _member_fstart(m: int) -> int:
        sub = skel_by_worm.get(int(m))
        return int(sub["frame_number"].iloc[0]) if sub is not None and len(sub) else (1 << 60)

    group_list: list[tuple[int, int, list[int]]] = []
    for gid, members in groups.items():
        members_sorted = sorted((int(m) for m in members), key=_member_fstart)
        group_list.append((_member_fstart(members_sorted[0]), int(gid), members_sorted))
    group_list.sort(key=lambda t: (t[0], t[1]))

    rows: list[dict] = []
    for _first_fstart, gid, member_ids in group_list:
        repr_id = member_ids[0]

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
        # A transition is only counted when the two consecutive non-paused frames
        # are (near-)adjacent (<= REVERSAL_MAX_GAP_FRAMES apart); a wider
        # separation means the forward->backward switch spans a tracking gap, so
        # it is discarded rather than imputed across the gap.
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
            adjacent = (seq_fr[1:] - seq_fr[:-1]) <= REVERSAL_MAX_GAP_FRAMES
            rev_mask = rev_mask & adjacent
            reversal_count = int(np.sum(rev_mask))
            # Frame at which backward motion begins = the reversal frame.
            reversal_frames = sorted(
                {int(f) for f in seq_fr[1:][rev_mask] if np.isfinite(f)}
            )

        # --- track duration: group span over the skeletons member frames ---
        member_frames: list[np.ndarray] = []
        for m in member_ids:
            sub = skel_by_worm.get(m)
            if sub is not None:
                member_frames.append(sub["frame_number"].values.astype(float))
        all_mf = (np.concatenate(member_frames) if member_frames
                  else np.array([], dtype=float))
        all_mf = all_mf[np.isfinite(all_mf)]
        if len(all_mf) and fps > 0:
            track_duration_s = (float(all_mf.max()) - float(all_mf.min()) + 1.0) / fps
        else:
            track_duration_s = float("nan")

        # --- reversal rate: per OBSERVED minute (finite-speed frames), not span ---
        observed_min = (n / fps / 60.0) if fps > 0 else 0.0
        reversal_rate_per_min = (reversal_count / observed_min) if observed_min > 1e-9 else 0.0

        # --- centroid path geometry (combined, gap-aware) ---
        path_length_px, net_displacement_px, tortuosity = _combined_path_geometry(
            member_ids, traj_by_worm, fps
        )

        # --- skeleton coverage + longest single-fragment continuous run ---
        skeleton_coverage, longest_continuous_run_s = _skel_coverage_and_run(
            skel_by_worm, member_ids, fps, has_skel_col
        )

        # --- head-angle bpm + bend_interval_cv over the concatenated members ---
        # Observed-frame-only: each member's signal contributes its valid
        # skeleton frames; bpm divides by valid-frame seconds so gaps (within or
        # between members) simply do not count.
        bpm: float = 0.0
        bend_cv: float = float("nan")
        if skel_all is not None:
            half_bends = 0
            total_valid = 0
            all_peak_frames: list[float] = []
            for m in member_ids:
                wt = feat_traj_by_worm.get(m)
                if wt is None or "skeleton_id" not in wt.columns:
                    continue
                sig = compute_head_angle_signal(wt, skel_all, fps, head_angle_prominence)
                if sig is None:
                    continue
                half_bends += len(sig["pos_peaks"]) + len(sig["neg_peaks"])
                total_valid += sig["n_valid"]
                fns = sig["frame_nums"]
                all_peak_frames.extend(fns[sig["pos_peaks"]].tolist())
                all_peak_frames.extend(fns[sig["neg_peaks"]].tolist())
            duration_min = (total_valid / fps / 60.0) if fps > 0 else 0.0
            bpm = round((half_bends / 2.0) / duration_min, 2) if duration_min > 1e-9 else 0.0
            bend_cv = bend_interval_cv(np.array(all_peak_frames, dtype=float), fps)

        is_long = bool(np.isfinite(track_duration_s) and track_duration_s >= long_threshold_s)

        rows.append({
            "condition": condition,
            "plate": plate,
            "video_name": video_name,
            "worm_index": gid,
            "repr_tierpsy_id": repr_id,
            "member_tierpsy_ids": ";".join(str(m) for m in member_ids),
            "group_classification": "linked",
            "bpm": bpm,
            "bend_interval_cv": bend_cv,
            "is_long": is_long,
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
