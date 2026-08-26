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
seconds, and longest_continuous_run_s is the longest single member fragment
(tolerating only sub-1s skeleton-fitter hiccups, see LONGEST_RUN_BRIDGE_FRAMES).
Body-length-normalized speed/path companions (BL_COLS) divide each pixel metric
by a single per-video length scalar (plate_mean_length_px, the trimmed-mean worm
length on that plate) so cross-plate / cross-day magnification drift cancels while
real individual worm-size variation is retained.

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
    # Longest skeletonised run within a single member fragment, in seconds,
    # bridging internal skeleton gaps of <= LONGEST_RUN_BRIDGE_FRAMES.
    "longest_continuous_run_s",
    # Fraction of trajectory frames where Tierpsy extracted a skeleton.
    "skeleton_coverage",
]

# Activity / variability metrics derived from the same per-frame speed/length
# arrays (no extra Tierpsy data). These get a per-condition _median column ONLY
# (not mean/std), so they are kept separate from METRIC_COLS / AGG_COLS.
ACTIVITY_COLS: list[str] = [
    "mean_speed_when_moving",
    "activity_fraction_above_1pxs",
    "activity_fraction_above_3pxs",
    "activity_fraction_above_5pxs",
    "speed_cv",
    "length_cv",
]

# Columns aggregated per condition (engine BPM metrics + kinematics) with
# mean/median/std. ACTIVITY_COLS and BL_COLS are aggregated separately (median
# only).
AGG_COLS: list[str] = ["bpm", "bend_interval_cv"] + METRIC_COLS

# Body-length-normalized companions to the pixel-based speed / path metrics. Each
# is pixel_metric / plate_mean_length_px — a SINGLE per-video length scalar (the
# trimmed mean worm length on that plate, see _plate_mean_length), NOT each worm's
# own mean_length_px. This calibrates out plate-to-plate / day-to-day optical
# magnification (worm length per condition drifts 50-70% across days, 12-30%
# plate-to-plate within a day from agar-height differences) while LEAVING real
# individual worm-size variation in the signal. Units are body-lengths-per-second
# (BL/s) and body-lengths (BL). The activity_fraction_above_*_bls columns recompute
# the activity fractions against BL/s thresholds (0.05 / 0.10 / 0.20 BL/s — the
# standard C. elegans activity-level breakpoints), i.e. |speed| >= threshold *
# plate_mean_length_px per frame. All are NaN when plate_mean_length_px could not
# be computed. Aggregated per condition with a _median column only (like
# ACTIVITY_COLS).
BL_COLS: list[str] = [
    "mean_speed_bls",
    "mean_forward_speed_bls",
    "mean_backward_speed_bls",
    "mean_speed_when_moving_bls",
    "path_length_bl",
    "net_displacement_bl",
    "activity_fraction_above_0p05_bls",
    "activity_fraction_above_0p10_bls",
    "activity_fraction_above_0p20_bls",
]

# Velocity-arrow reversal / turn columns (see ARROW_* constants and
# _velocity_arrow_events). These sit immediately after reversal_rate_per_min for
# side-by-side comparison with the motion_mode-based reversal_count, and are
# aggregated per condition with a _median column only (like ACTIVITY_COLS).
ARROW_COLS: list[str] = [
    "arrow_reversal_count",
    "arrow_reversal_rate_per_min",
    "turn_count",
    "turn_rate_per_min",
]

# Per-worm columns inserted into the sheet immediately after an anchor column for
# readability: each BL column follows its pixel-based counterpart, and the
# per-video plate_mean_length_px (the BL denominator, repeated for every worm in a
# video) follows the per-worm mean_length_px it is contrasted with.
# (anchor_col, [cols_inserted_after_it]).
_EXTRA_AFTER: list[tuple[str, list[str]]] = [
    ("reversal_rate_per_min", ARROW_COLS),
    ("mean_speed_pxs", ["mean_speed_bls"]),
    ("mean_forward_speed_pxs", ["mean_forward_speed_bls"]),
    ("mean_backward_speed_pxs", ["mean_backward_speed_bls"]),
    ("path_length_px", ["path_length_bl"]),
    ("net_displacement_px", ["net_displacement_bl"]),
    ("mean_length_px", ["plate_mean_length_px"]),
    ("mean_speed_when_moving", ["mean_speed_when_moving_bls"]),
    ("activity_fraction_above_5pxs",
     ["activity_fraction_above_0p05_bls",
      "activity_fraction_above_0p10_bls",
      "activity_fraction_above_0p20_bls"]),
]


def _interleave_extra_cols(base: list[str]) -> list[str]:
    """Insert each extra column right after its anchor column in `base`."""
    out = list(base)
    for anchor, extras in _EXTRA_AFTER:
        i = out.index(anchor)
        out[i + 1:i + 1] = extras
    return out


# Boolean quality flag appended to every per-worm row (see _passes_filter).
QUALITY_COL: str = "passed_filter"

PER_WORM_COLS: list[str] = _interleave_extra_cols(
    ID_COLS + ENGINE_COLS + METRIC_COLS + ACTIVITY_COLS
) + [QUALITY_COL]

# Fraction of the video-wide median |speed| below which a frame counts as paused.
_PAUSED_FRACTION_OF_MEDIAN = 0.10   # legacy, only the no-length fallback

# "Moving" starts here, in body-lengths per second. Fixed across videos and
# conditions on purpose — see the paused-threshold note in
# compute_crawling_metrics.
PAUSED_BL_PER_S: float = 0.01

# A worm typically PAUSES briefly (measured motion_mode == 0) between forward and
# backward motion during a real reversal — that pause is part of the reversal
# event, not a data gap. A forward->backward transition still counts as a reversal
# if the forward and backward runs are separated by <= this many consecutive
# measured-paused frames. DATA GAPS (motion_mode NaN — no skeleton/measurement)
# always disqualify the transition: we cannot know what happened there.
# 60 frames = 2.0s at 30fps.
REVERSAL_PAUSE_TOLERANCE_FRAMES: int = 60

# ---------------------------------------------------------------------------
# Velocity-arrow reversal / turn detection (see _velocity_arrow_events).
#
# A second, motion_mode-INDEPENDENT reversal detector built from the centroid
# velocity vector. The existing motion_mode-based reversal_count misses two
# cases: worms twitching in place (rapid fwd<->bwd oscillation never sustains a
# directional segment) and near-stationary worms (every frame falls below
# Tierpsy's speed threshold and is classed "paused", so reversals read as 0). The
# arrow method computes a centered finite-difference velocity per frame, compares
# the heading LOOKAHEAD frames before vs after each frame, and fires an event on
# big direction changes (>= REVERSAL_THRESHOLD_DEG = reversal; TURN..REVERSAL =
# turn) with non-maximum suppression. These columns live ALONGSIDE the existing
# reversal_count for at least one analysis cycle of side-by-side comparison; the
# old metric is unchanged. Frame counts derive from these seconds via fps inside
# the function.
# ---------------------------------------------------------------------------
ARROW_SMOOTH_HALF_S: float = 0.3       # centered velocity smoothing half-window
ARROW_LOOKAHEAD_S: float = 0.5         # before/after heading comparison offset
ARROW_EVENT_SEPARATION_S: float = 0.5  # min gap between consecutive events
ARROW_SUSTAIN_HALF_S: float = 0.25     # local-max (NMS) half-window
ARROW_MIN_SPEED_BL_PER_FRAME: float = 0.001  # heading undefined (NaN) below this;
#   body-lengths/frame, scaled by plate_mean_length_px so the physical cutoff is
#   constant across plates/days despite magnification drift. 0.001 BL/frame =
#   0.10 px/frame at day-0 (~100 px/worm); sweep optimum (0.07-0.10 px/frame best
#   S/N for slow reversals; <0.05 explodes with stationary-worm noise).
_ARROW_MIN_SPEED_PX_PER_FRAME_FALLBACK: float = 0.1  # used when plate length unknown
ARROW_REVERSAL_THRESHOLD_DEG: float = 140.0
ARROW_TURN_THRESHOLD_DEG: float = 60.0

# Internal skeleton gaps of this many frames or fewer do NOT break a worm's
# longest_continuous_run_s. Tierpsy's skeleton fitter hiccups for a frame or two
# every 10-20s on a cleanly-tracked worm; without bridging, those single-frame
# dropouts shatter the run counter. longest_continuous_run_s is no longer the
# quality gate (the gate is now track span + skeleton coverage, see
# _passes_filter), but it remains an information column, so we bridge generously:
# 30 frames = 1.0s at 30fps captures flickers of up to a second on a worm that
# stays the same animal at the same location, while still breaking on any longer
# (potentially biological) pause. Gaps at the very start/end of a fragment are NOT
# bridged (those are fragment boundaries, not hiccups).
LONGEST_RUN_BRIDGE_FRAMES: int = 30

# ---------------------------------------------------------------------------
# Quality-filter thresholds — tune freely; raw per-worm rows are always kept,
# so changing these and re-aggregating needs no Tierpsy re-run.
# The gate is span + coverage: a worm passes if it was on the plate for
# >= min_span_s with >= SKELETON_COVERAGE_MIN of its frames skeletonised. This
# replaces the old "unbroken longest run >= N" gate, which rejected clearly
# tracked but flickery worms (180s span, 80% coverage, intermittent skeleton
# hiccups). MIN_SPAN_S is the fallback minimum span; callers may override it per
# run (e.g. from the UI "Min track span (s)" input) by passing min_span_s through
# compute_crawling_metrics / aggregate_per_condition.
# ---------------------------------------------------------------------------
MIN_SPAN_S: float = 10.0
# Retained for reference and for anything that still imports it. NOT a gate any
# more — see _passes_filter.
SKELETON_COVERAGE_MIN: float = 0.70

# ---------------------------------------------------------------------------
# Per-video body-length calibration (see _plate_mean_length). The BL-normalized
# columns divide by ONE length scalar per video (the plate's optical
# magnification), not each worm's own length, so individual size variation stays
# in the signal. The scalar is the trimmed mean of worm lengths, restricted to
# worms tracked long enough for a stable length estimate. These are calibration
# parameters, NOT a track-quality gate (kept worms are not used — that would bias
# the calibration toward slower worms).
# ---------------------------------------------------------------------------
BL_CALIB_MIN_SPAN_S: float = 10.0   # min track span for a worm to count toward the scalar
BL_CALIB_MIN_WORMS: int = 5         # below this many, fall back to all worms' lengths
BL_CALIB_TRIM_FRAC: float = 0.10    # drop bottom/top 10% before averaging


def _passes_filter(track_duration_s, skeleton_coverage=None,
                   min_span_s: float | None = None) -> bool:
    """
    Return True if a track is long enough to enter the aggregate.

    LENGTH ONLY. skeleton_coverage is accepted and ignored — it stays in the
    per-worm sheet as an information column.

    The old gate also required skeleton_coverage >= 0.70. On the day-1 N2 10J
    reference video the plate-wide coverage was 0.692, so that floor sat
    exactly on the middle of the distribution and behaved as a coin flip: it
    cut 40 qualifying tracks to 15 and 2153 worm-seconds to 1084. Worse, it was
    not random — coverage fell with distance from the frame centre, because the
    illumination gradient cost skeletons at the rim, so the gate preferentially
    deleted worms for where they were standing rather than for anything about
    them. The gradient is now corrected at transcode (see analysis/flatfield.py)
    and coverage runs ~0.91, but a coverage floor would still throw away worms
    for coiling and for passing a neighbour, which are behaviours and not
    defects.

    Metrics never impute across gaps — speed fractions average observed frames
    only, path length sums frame-adjacent steps only, bpm divides by
    valid-skeleton seconds — so a low-coverage track is noisier but not biased,
    and its coverage is on the row for anyone who wants to filter on it.
    """
    try:
        span = float(track_duration_s)
    except (TypeError, ValueError):
        return False
    try:
        threshold = float(min_span_s)
        if not np.isfinite(threshold):
            threshold = MIN_SPAN_S
    except (TypeError, ValueError):
        threshold = MIN_SPAN_S
    return bool(np.isfinite(span) and span >= threshold)


def _mean_finite(arr: np.ndarray) -> float:
    arr = arr[np.isfinite(arr)]
    return float(np.mean(arr)) if len(arr) else float("nan")


def _per_bodylength(value, length_px) -> float:
    """
    value / length_px, with NaN guards.

    length_px is the per-video plate_mean_length_px (the BL denominator). Returns
    NaN if length_px is missing, non-finite, zero/negative, or value is
    non-finite — so every BL-normalized column is NaN-safe.
    """
    try:
        ml = float(length_px)
        v = float(value)
    except (TypeError, ValueError):
        return float("nan")
    if not (np.isfinite(ml) and ml > 0 and np.isfinite(v)):
        return float("nan")
    return v / ml


def _trimmed_mean(vals: np.ndarray, trim_frac: float) -> float:
    """
    Mean of `vals` after dropping the lowest and highest `trim_frac` fraction.

    Drops floor(n * trim_frac) values from each end (matching scipy.trim_mean). If
    trimming would remove everything (very small n), falls back to the plain mean.
    Returns NaN for an empty / all-non-finite input.
    """
    v = np.sort(vals[np.isfinite(vals)])
    n = len(v)
    if n == 0:
        return float("nan")
    k = int(np.floor(n * trim_frac))
    core = v[k:n - k] if (n - 2 * k) > 0 else v
    return float(np.mean(core))


def _plate_mean_length(lengths: np.ndarray, spans: np.ndarray) -> float:
    """
    One per-video length scalar for BL normalization (plate optical magnification).

    lengths / spans are the per-worm mean_length_px and track_duration_s for every
    GROUPED worm in the video (NOT just kept worms — that would bias the scalar
    toward slower movers). Worms with span >= BL_CALIB_MIN_SPAN_S are preferred
    (their length estimate is stable); if fewer than BL_CALIB_MIN_WORMS qualify,
    all worms' lengths are used as a fallback. The scalar is the trimmed mean
    (BL_CALIB_TRIM_FRAC off each end) to resist developmental outliers. Returns
    NaN if no finite lengths exist.
    """
    lengths = np.asarray(lengths, dtype=float)
    spans = np.asarray(spans, dtype=float)
    finite = np.isfinite(lengths)
    stable = finite & np.isfinite(spans) & (spans >= BL_CALIB_MIN_SPAN_S)
    use = lengths[stable] if int(np.sum(stable)) >= BL_CALIB_MIN_WORMS else lengths[finite]
    if len(use) == 0:
        return float("nan")
    return _trimmed_mean(use, BL_CALIB_TRIM_FRAC)


def _longest_skeletonized_run_frames(has_skeleton: np.ndarray,
                                     bridge_frames: int) -> int:
    """
    Longest run of skeletonised frames, allowing internal gaps of <=bridge_frames.

    has_skeleton is a per-FRAME boolean array (one entry per frame over a single
    fragment's [f_start, f_end] span — missing frames and unskeletonised frames
    are both False). Any False-run of length <= bridge_frames that is bounded by
    True on BOTH sides is treated as continuous (a skeleton-fitter hiccup). A
    False-run at the very start or end of the array is a fragment boundary, not a
    hiccup, and is NOT bridged. Returns the longest True-run length (frames).
    """
    n = len(has_skeleton)
    if n == 0:
        return 0
    sk = has_skeleton.astype(bool).copy()
    i = 0
    while i < n:
        if not sk[i]:
            j = i
            while j < n and not sk[j]:
                j += 1
            gap_len = j - i
            if i > 0 and j < n and gap_len <= bridge_frames:  # internal gap only
                sk[i:j] = True
            i = j
        else:
            i += 1
    longest = current = 0
    for v in sk:
        if v:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def _member_bridged_run_s(frames: np.ndarray, sk_mask: np.ndarray,
                          fps: float, bridge_frames: int) -> float:
    """
    Longest bridged skeletonised run for ONE member fragment, in seconds.

    frames    — frame numbers for the member's rows (sorted ascending).
    sk_mask   — per-row boolean: True where the frame was skeletonised.
    Builds a per-frame boolean over the member's [min, max] frame span (so a
    Tierpsy join-gap of missing frames > bridge_frames correctly breaks the run,
    not just unskeletonised rows), then applies _longest_skeletonized_run_frames.
    """
    finite = np.isfinite(frames)
    fr = frames[finite].astype(np.int64)
    if len(fr) == 0 or fps <= 0:
        return float("nan")
    skm = sk_mask[finite]
    f0, f1 = int(fr.min()), int(fr.max())
    arr = np.zeros(f1 - f0 + 1, dtype=bool)
    arr[fr[skm] - f0] = True  # mark skeletonised frames on the per-frame axis
    return _longest_skeletonized_run_frames(arr, bridge_frames) / fps


def _count_reversals_with_pause_tolerance(
    motion_mode: np.ndarray, pause_tolerance_frames: int,
) -> tuple[int, list[int]]:
    """
    Count forward->backward transitions in a per-frame motion_mode array.

    motion_mode is indexed by frame (1 = forward, -1 = backward, 0 = measured
    paused, NaN = data gap / no measurement). A transition counts as a reversal
    when, walking backward from a backward run, the most recent measured frame is
    forward (1), separated only by <= pause_tolerance_frames measured-paused (0)
    frames — a brief pause is part of the reversal, not a gap. A NaN frame in
    between (we cannot know what happened) disqualifies the transition. Each
    backward run is counted at most once.

    Returns (n_reversals, reversal_frame_indices) where each index is the start
    frame of a counted backward run (the frame at which the reversal fires).
    """
    mm = np.asarray(motion_mode, dtype=float)
    n = len(mm)
    n_rev = 0
    rev_idx: list[int] = []
    i = 0
    while i < n:
        if mm[i] == -1:
            # Walk back to the most recent non-paused, non-NaN frame.
            j = i - 1
            paused_run = 0
            saw_gap = False
            while j >= 0:
                if np.isnan(mm[j]):
                    saw_gap = True
                    break
                elif mm[j] == 0:
                    paused_run += 1
                    if paused_run > pause_tolerance_frames:
                        break
                    j -= 1
                elif mm[j] == 1:  # forward — a real reversal unless a gap intervened
                    if not saw_gap and paused_run <= pause_tolerance_frames:
                        n_rev += 1
                        rev_idx.append(i)
                    break
                else:  # mm[j] == -1, previous backward run; not a new reversal
                    break
            # Skip to the end of this backward run so we do not double-count it.
            while i < n and mm[i] == -1:
                i += 1
        else:
            i += 1
    return n_rev, rev_idx


def _skel_coverage_and_run(
    skel_by_worm: dict, member_ids: list[int], fps: float, has_skel_col: bool,
) -> tuple[float, float]:
    """
    Skeleton coverage + longest continuous run over a grouped worm's members.

    skeleton_coverage        — mean of has_skeleton across all member frames
                               (NaN if no has_skeleton column / no members).
    longest_continuous_run_s — longest skeletonised run within a SINGLE member
                               fragment (NOT bridged across members), in seconds,
                               where internal skeleton gaps of
                               <= LONGEST_RUN_BRIDGE_FRAMES are treated as
                               continuous (skeleton-fitter hiccups). The linker may
                               chain many short fragments into one identity, so the
                               run is taken per-member: it reports how long the worm
                               was tracked UNBROKEN (modulo sub-1s hiccups). This is
                               now an information column only — the quality gate
                               uses track span + coverage, not this run.
    """
    all_hs: list[np.ndarray] = []
    longest_run_s = 0.0
    have_any = False
    for mid in member_ids:
        sub = skel_by_worm.get(mid)
        if sub is None:
            continue
        have_any = True
        frames = sub["frame_number"].values.astype(float)
        if has_skel_col:
            hs = sub["has_skeleton"].values.astype(float)
            all_hs.append(hs)
            sk_mask = np.isfinite(hs) & (hs >= 0.5)  # skeletonised frames only
        else:
            # No has_skeleton column: treat every tracked frame as skeletonised.
            sk_mask = np.ones(len(frames), dtype=bool)
        run_s = _member_bridged_run_s(frames, sk_mask, fps, LONGEST_RUN_BRIDGE_FRAMES)
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
        tdf = traj_by_worm.get(mid)
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


def _combined_centroid_track(
    member_ids: list[int], skel_by_worm: dict,
) -> tuple[int | None, np.ndarray, np.ndarray]:
    """
    Dense per-frame centroid (coord_x / coord_y) track for a grouped worm.

    Sourced from the *_skeletons.hdf5 trajectories_data (skel_by_worm) — the SAME
    table and coordinate space the renderer plots on — so the velocity arrow lines
    up with the rendered video frames (indexed by frame_number). Concatenates the
    member fragments' centroids and lays them onto a contiguous per-frame axis
    spanning [f0, f1]; frames with no centroid (gaps within or between fragments)
    stay NaN and are NEVER interpolated, so the velocity arrow and its derived
    events propagate NaN across gaps. Returns (f0, x_dense, y_dense); f0 is None
    (arrays empty) when no finite centroid exists or coord columns are absent.
    """
    fxy: list[tuple[int, float, float]] = []
    for mid in member_ids:
        sub = skel_by_worm.get(mid)
        if sub is None or "coord_x" not in sub.columns or "coord_y" not in sub.columns:
            continue
        fr = sub["frame_number"].values.astype(float)
        cx = sub["coord_x"].values.astype(float)
        cy = sub["coord_y"].values.astype(float)
        m = np.isfinite(fr) & np.isfinite(cx) & np.isfinite(cy)
        for f, x, y in zip(fr[m], cx[m], cy[m]):
            fxy.append((int(f), float(x), float(y)))
    if not fxy:
        return None, np.array([], dtype=float), np.array([], dtype=float)
    f0 = min(t[0] for t in fxy)
    f1 = max(t[0] for t in fxy)
    x_dense = np.full(f1 - f0 + 1, np.nan)
    y_dense = np.full(f1 - f0 + 1, np.nan)
    for f, x, y in fxy:
        x_dense[f - f0] = x  # last write wins on the (rare) duplicate frame
        y_dense[f - f0] = y
    return f0, x_dense, y_dense


def _velocity_arrow_events(x: np.ndarray, y: np.ndarray, fps: float,
                           plate_mean_length_px: float | None = None) -> dict:
    """
    Centered velocity arrow + reversal / turn event detection on a centroid track.

    x / y are DENSE per-frame centroid arrays (NaN at gaps; index 0 == the track's
    f0). Returns a dict with:
      vx, vy                      — per-frame centered-difference velocity (px/frame),
                                    NaN where either centered endpoint is NaN/off-array.
      reversal_offsets/turn_offsets — event indices into the dense arrays (add f0
                                    for absolute frame numbers).
      reversal_count / turn_count — event counts.
      reversal_rate_per_min / turn_rate_per_min — events per OBSERVED minute, where
                                    observed_s = (# finite-velocity frames) / fps;
                                    NaN if that is 0.

    Method (all windows derive from fps):
      1. velocity arrow: centered finite difference over ±SMOOTH_HALF frames, so
         the arrow reflects motion AT frame i (not a past-only lag).
      2. heading change: angle between the unit velocity LOOKAHEAD frames before
         and after i; NaN when either side's speed < MIN_SPEED_PX_PER_FRAME.
      3. events: walk the angle array, keep a local maximum >= TURN_THRESHOLD_DEG
         separated from the last event by >= EVENT_SEPARATION frames (NMS), classed
         reversal (>= REVERSAL_THRESHOLD_DEG) or turn otherwise.
    """
    n = len(x)
    vx = np.full(n, np.nan)
    vy = np.full(n, np.nan)
    empty = {
        "vx": vx, "vy": vy,
        "reversal_offsets": [], "turn_offsets": [],
        "reversal_count": 0, "turn_count": 0,
        "reversal_rate_per_min": float("nan"), "turn_rate_per_min": float("nan"),
    }
    if n == 0 or not (fps > 0):
        return empty

    # Body-length-relative minimum speed: convert BL/frame -> px/frame using the
    # per-video plate scalar so the physical cutoff is constant across plates/days
    # (magnification drift no longer biases which motions count). Fall back to a
    # fixed px/frame value when calibration is unavailable.
    if (plate_mean_length_px is None or not np.isfinite(plate_mean_length_px)
            or plate_mean_length_px <= 0):
        min_speed_px_per_frame = _ARROW_MIN_SPEED_PX_PER_FRAME_FALLBACK
    else:
        min_speed_px_per_frame = ARROW_MIN_SPEED_BL_PER_FRAME * plate_mean_length_px

    # --- Step 1: centered finite-difference velocity arrow ---
    sh = max(3, int(round(ARROW_SMOOTH_HALF_S * fps)))
    idx = np.arange(n)
    a = idx - sh
    b = idx + sh
    inb = (a >= 0) & (b < n)
    ia = a[inb]
    ib = b[inb]
    fin = (np.isfinite(x[ia]) & np.isfinite(x[ib])
           & np.isfinite(y[ia]) & np.isfinite(y[ib]))
    pos = idx[inb][fin]
    vx[pos] = (x[ib][fin] - x[ia][fin]) / (2.0 * sh)
    vy[pos] = (y[ib][fin] - y[ia][fin]) / (2.0 * sh)

    # --- Step 2: lookahead-based heading change (degrees) ---
    look = max(1, int(round(ARROW_LOOKAHEAD_S * fps)))
    angle = np.full(n, np.nan)
    if n - 2 * look > 0:
        c = np.arange(look, n - look)
        v0x = vx[c - look]; v0y = vy[c - look]
        v1x = vx[c + look]; v1y = vy[c + look]
        s0 = np.hypot(v0x, v0y)
        s1 = np.hypot(v1x, v1y)
        good = (np.isfinite(s0) & np.isfinite(s1)
                & (s0 >= min_speed_px_per_frame)
                & (s1 >= min_speed_px_per_frame))
        with np.errstate(invalid="ignore", divide="ignore"):
            dot = (v0x * v1x + v0y * v1y) / (s0 * s1)
        dot = np.clip(dot, -1.0, 1.0)
        ang = np.degrees(np.arccos(dot))
        angle[c[good]] = ang[good]

    # --- Step 3: event detection with non-maximum suppression ---
    sep = max(1, int(round(ARROW_EVENT_SEPARATION_S * fps)))
    sustain = max(1, int(round(ARROW_SUSTAIN_HALF_S * fps)))
    reversal_offsets: list[int] = []
    turn_offsets: list[int] = []
    last_event = -(10 ** 9)
    for i in range(n):
        ai = angle[i]
        if not np.isfinite(ai) or ai < ARROW_TURN_THRESHOLD_DEG:
            continue
        if i - last_event < sep:
            continue
        lo = max(0, i - sustain)
        hi = min(n, i + sustain)
        win = angle[lo:hi]
        if np.any(np.isfinite(win)) and ai < np.nanmax(win):
            continue  # not a local maximum
        if ai >= ARROW_REVERSAL_THRESHOLD_DEG:
            reversal_offsets.append(i)
        else:
            turn_offsets.append(i)
        last_event = i

    # --- Step 4: per-minute rates over observed (finite-velocity) seconds ---
    n_finite = int(np.sum(np.isfinite(vx)))
    observed_s = n_finite / fps
    if observed_s > 0:
        rev_rate = len(reversal_offsets) / observed_s * 60.0
        turn_rate = len(turn_offsets) / observed_s * 60.0
    else:
        rev_rate = float("nan")
        turn_rate = float("nan")

    return {
        "vx": vx, "vy": vy,
        "reversal_offsets": reversal_offsets, "turn_offsets": turn_offsets,
        "reversal_count": len(reversal_offsets), "turn_count": len(turn_offsets),
        "reversal_rate_per_min": rev_rate, "turn_rate_per_min": turn_rate,
    }


def compute_crawling_metrics(
    hdf5_path: Path,
    fps: float,
    condition: str,
    plate: str,
    video_name: str,
    head_angle_prominence: float = 0.30,
    long_threshold_s: float = 5.0,
    min_span_s: float | None = None,
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
    timeseries_data cannot be read. long_threshold_s drives is_long; min_span_s
    sets the passed_filter minimum-span threshold (None falls back to
    MIN_SPAN_S). When engine_log_out is provided, a linker analysis_log
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

    groups, refused, linker_log, traj_split = link_fragments(skel_traj, fps)
    n_fragments = int(skel_traj["worm_index_joined"].nunique())
    if not groups:
        log.warning("crawling: linker produced no groups for %s", skeletons_path)
        if engine_log_out is not None:
            engine_log_out.update(linker_log)
        return []
    if engine_log_out is not None:
        engine_log_out.update(linker_log)

    has_skel_col = "has_skeleton" in skel_traj.columns
    if not has_skel_col:
        log.warning("crawling: %s trajectories_data has no has_skeleton column", skeletons_path)

    # Everything downstream is keyed on frag_id, not on worm_index_joined: a
    # fragment cut at a merge episode yields two pieces that share one Tierpsy
    # id but are different tracks, so indexing by the Tierpsy id would splice
    # them back together and re-admit the merged frames we just removed.
    skel_by_worm: dict[str, "pd.DataFrame"] = {
        fid: sub.sort_values("frame_number")
        for fid, sub in traj_split.groupby("frag_id")
    }
    # frag_id -> (originating Tierpsy worm_index_joined, first frame, last frame)
    frag_meta: dict[str, tuple] = {}
    for fid, sub in skel_by_worm.items():
        frag_meta[fid] = (int(sub["worm_index_joined"].iloc[0]),
                          int(sub["frame_number"].iloc[0]),
                          int(sub["frame_number"].iloc[-1]))

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

    # Paused threshold: a FIXED speed in body-lengths per second, scaled only by
    # the worms' apparent size on this plate.
    #
    # This used to be 10% of the median |speed| across all worms in THIS video.
    # Every video therefore re-normalised to itself, and conditions live in
    # different videos — so if a UV dose halved every worm's speed, the
    # threshold halved with it and fraction_paused came out unchanged. The same
    # scalar drives fraction_forward, fraction_backward and
    # mean_speed_when_moving, so the whole activity block was blind to exactly
    # the effect it was there to measure.
    #
    # Dividing by worm length instead keeps the magnification correction (a
    # property of the optics) while dropping the behaviour normalisation (a
    # property of the treatment). PAUSED_BL_PER_S = 0.01 reproduces the old
    # threshold on the day-1 N2 10J reference video (median |speed| 12.98 px/s,
    # median length 136.9 px -> old threshold 1.298 px/s = 0.0095 BL/s), so
    # existing numbers are comparable while dose effects can now show through.
    if "length" in ts.columns:
        _len = ts["length"].values.astype(float)
        _len = _len[np.isfinite(_len) & (_len > 0)]
        _plate_len = float(np.median(_len)) if len(_len) else float("nan")
    else:
        _plate_len = float("nan")
    if np.isfinite(_plate_len) and _plate_len > 0:
        paused_threshold = PAUSED_BL_PER_S * _plate_len
    else:
        # No length anywhere: fall back to the old self-normalising rule rather
        # than to a pixel constant that would be meaningless at another
        # magnification. Logged, because the activity columns are then
        # dose-insensitive for this video.
        if "speed" in ts.columns:
            _sp = np.abs(ts["speed"].values.astype(float))
            _sp = _sp[np.isfinite(_sp)]
            paused_threshold = _PAUSED_FRACTION_OF_MEDIAN * (
                float(np.median(_sp)) if len(_sp) else 0.0)
        else:
            paused_threshold = 0.0
        log.warning("crawling: no worm length in %s; paused threshold fell back "
                    "to the per-video self-normalising rule", hdf5_path)

    # Pre-index timeseries + featuresN trajectory rows by Tierpsy worm_index
    # (worm_index == trajectories worm_index_joined == linker member id).
    _ts_by_tid: dict[int, "pd.DataFrame"] = {
        int(wi): g.sort_values(ts_frame_col) for wi, g in ts.groupby("worm_index")
    }
    _traj_by_tid: dict[int, "pd.DataFrame"] = {}
    traj_frame_col = "frame_number"
    if feat_traj is not None and "worm_index_joined" in feat_traj.columns:
        traj_frame_col = ("timestamp_raw" if "timestamp_raw" in feat_traj.columns
                          else "frame_number")
        for wi, g in feat_traj.groupby("worm_index_joined"):
            _traj_by_tid[int(wi)] = g.sort_values(traj_frame_col).reset_index(drop=True)

    # Re-key onto frag_id, clipped to each piece's own frame window so a split
    # fragment's two tracks never see each other's rows (nor the merged frames
    # between them).
    def _slice(by_tid: dict, frame_col: str) -> dict:
        out: dict[str, "pd.DataFrame"] = {}
        for fid, (tid, f0, f1) in frag_meta.items():
            src = by_tid.get(tid)
            if src is None or frame_col not in src.columns:
                continue
            fr = src[frame_col].values.astype(float)
            sel = src[(fr >= f0) & (fr <= f1)]
            if len(sel):
                out[fid] = sel.reset_index(drop=True)
        return out

    ts_by_worm = _slice(_ts_by_tid, ts_frame_col)
    traj_by_worm = _slice(_traj_by_tid, traj_frame_col)   # path geometry (coords)
    feat_traj_by_worm = traj_by_worm                       # head-angle (skeleton_id)

    # Order groups by their earliest fragment start so worm_index / row order is
    # stable run-to-run (the union-find group id is otherwise arbitrary). Each
    # group's members are ordered by start frame; member[0] is the representative.
    def _member_fstart(m) -> int:
        sub = skel_by_worm.get(m)
        return int(sub["frame_number"].iloc[0]) if sub is not None and len(sub) else (1 << 60)

    # Group ids from the linker are opaque frag_ids. Order groups by their
    # earliest fragment so worm_index is stable run to run, then number them.
    _tmp: list[tuple[int, str, list]] = []
    for gid, members in groups.items():
        members_sorted = sorted(members, key=_member_fstart)
        _tmp.append((_member_fstart(members_sorted[0]), str(gid), members_sorted))
    _tmp.sort(key=lambda t: (t[0], t[1]))
    group_list: list[tuple[int, int, list]] = [
        (fs, i, mem) for i, (fs, _g, mem) in enumerate(_tmp)
    ]

    rows: list[dict] = []
    abs_speed_by_row: list[np.ndarray] = []
    for _first_fstart, gid, member_ids in group_list:
        # Reported ids stay Tierpsy's, so the renders and the worm_index_map in
        # crawling.py keep working; deduped because a split fragment's pieces
        # share one id.
        _tids = [frag_meta[m][0] for m in member_ids if m in frag_meta]
        _seen: set = set()
        member_tids = [t for t in _tids if not (t in _seen or _seen.add(t))]
        repr_id = member_tids[0] if member_tids else -1

        # --- combined timeseries track over all members ---
        member_ts = [ts_by_worm[m] for m in member_ids if m in ts_by_worm]  # frag-sliced
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

        # --- activity / variability metrics (from the same per-frame arrays) ---
        # All over finite-speed (skeletonised) frames. moving_threshold reuses the
        # video-level paused threshold so "moving" is exactly "not paused".
        moving = abs_speed[abs_speed >= paused_threshold]
        mean_speed_when_moving = float(np.mean(moving)) if len(moving) else float("nan")
        if n:
            activity_fraction_above_1pxs = float(np.mean(abs_speed >= 1.0))
            activity_fraction_above_3pxs = float(np.mean(abs_speed >= 3.0))
            activity_fraction_above_5pxs = float(np.mean(abs_speed >= 5.0))
            mean_abs_speed = float(np.nanmean(abs_speed))
            speed_cv = (float(np.nanstd(abs_speed) / mean_abs_speed)
                        if mean_abs_speed > 0 else float("nan"))
        else:
            activity_fraction_above_1pxs = float("nan")
            activity_fraction_above_3pxs = float("nan")
            activity_fraction_above_5pxs = float("nan")
            speed_cv = float("nan")
        length_arr = (g["length"].values.astype(float)
                      if "length" in g.columns else np.array([], dtype=float))
        length_arr = length_arr[np.isfinite(length_arr)]
        if len(length_arr):
            mean_length_px = float(np.nanmean(length_arr))
            length_cv = (float(np.nanstd(length_arr) / mean_length_px)
                         if mean_length_px > 0 else float("nan"))
        else:
            mean_length_px = float("nan")
            length_cv = float("nan")

        # --- pixel speed scalars (BL companions are filled in a second pass once
        # the per-video plate_mean_length_px is known — see below the loop). ---
        mean_speed_pxs = _mean_finite(abs_speed)
        # nan, NOT 0.0, when the worm contributed no frames of that sign.
        # aggregate_per_condition filters non-finite values but not zeros, so a
        # zero here is averaged in as "this worm crawled at 0 px/s" — a
        # mechanical downward bias, worst in exactly the conditions where
        # tracking is worst, which manufactures a dose response out of a
        # tracking artefact.
        mean_forward_speed_pxs = float(np.mean(fwd)) if len(fwd) else float("nan")
        mean_backward_speed_pxs = (float(np.mean(np.abs(bwd))) if len(bwd)
                                   else float("nan"))

        # --- reversal_count + reversal_frames: forward->backward transitions ---
        # Build a per-FRAME motion_mode array over the worm's [f0, f1] span:
        # measured frames carry their motion_mode (1 fwd / -1 bwd / 0 paused),
        # frames with no timeseries row (or a NaN motion_mode) become NaN data
        # gaps. A brief MEASURED pause between forward and backward is part of the
        # reversal (tolerated up to REVERSAL_PAUSE_TOLERANCE_FRAMES); a data gap
        # is not (we cannot know what happened across it).
        # A worm that WAS measured and did not reverse genuinely scores 0; a
        # worm with no measured frames at all must not, so this starts as nan
        # and only becomes a real count once there is something to count.
        reversal_count: float = float("nan")
        reversal_frames: list[int] = []
        frames_ts = (g[ts_frame_col].values.astype(float)
                     if ts_frame_col in g.columns else np.array([], dtype=float))
        if has_motion_mode and "motion_mode" in g.columns:
            mm_src = g["motion_mode"].values.astype(float)
        else:
            # Fallback when motion_mode is absent: derive a motion_mode-like code
            # from speed sign, using the same paused threshold as fraction_paused.
            sp = (g["speed"].values.astype(float)
                  if "speed" in g.columns else np.full(len(frames_ts), np.nan))
            mm_src = np.where(np.abs(sp) < paused_threshold, 0.0, np.sign(sp))
            mm_src[~np.isfinite(sp)] = np.nan
        fin = np.isfinite(frames_ts)
        if fin.any():
            fr = frames_ts[fin].astype(np.int64)
            f0, f1 = int(fr.min()), int(fr.max())
            mm_frame = np.full(f1 - f0 + 1, np.nan)
            mm_frame[fr - f0] = mm_src[fin]
            reversal_count, rev_offsets = _count_reversals_with_pause_tolerance(
                mm_frame, REVERSAL_PAUSE_TOLERANCE_FRAMES
            )
            reversal_frames = sorted(f0 + off for off in rev_offsets)

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
        reversal_rate_per_min = ((reversal_count / observed_min)
                                 if observed_min > 1e-9 else float("nan"))

        # --- centroid path geometry (combined, gap-aware) ---
        path_length_px, net_displacement_px, tortuosity = _combined_path_geometry(
            member_ids, traj_by_worm, fps
        )

        # --- skeleton coverage + longest single-fragment continuous run ---
        skeleton_coverage, longest_continuous_run_s = _skel_coverage_and_run(
            skel_by_worm, member_ids, fps, has_skel_col
        )

        # --- velocity-arrow reversal / turn detection (motion_mode-independent) ---
        # Built on the dense centroid track from *_skeletons.hdf5 (same source the
        # renderer plots). Event offsets are indices into the dense array; add
        # arrow_f0 for absolute frame numbers (passed to the renderer below).
        # Only the dense centroid track is built here; the velocity-arrow EVENTS
        # need the per-video plate_mean_length_px (BL-relative speed cutoff), which
        # is computed after this loop, so they are filled in the second pass below.
        arrow_f0, arrow_x, arrow_y = _combined_centroid_track(member_ids, skel_by_worm)

        # --- head-angle bpm + bend_interval_cv over the concatenated members ---
        # Observed-frame-only: each member's signal contributes its valid
        # skeleton frames; bpm divides by valid-frame seconds so gaps (within or
        # between members) simply do not count.
        bpm: float = float("nan")
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
            # duration_min == 0 means no member yielded a usable head-angle
            # signal, i.e. this worm was never measured. That is NaN, not 0 —
            # a 0 here is averaged into the condition mean as a real bend rate.
            bpm = (round((half_bends / 2.0) / duration_min, 2)
                   if duration_min > 1e-9 else float("nan"))
            bend_cv = bend_interval_cv(np.array(all_peak_frames, dtype=float), fps)

        is_long = bool(np.isfinite(track_duration_s) and track_duration_s >= long_threshold_s)

        # A worm with no timeseries rows at all was NOT measured. Tierpsy's
        # SKE_FILT (filt_min_displacement=100 for crawling) drops whole
        # fragments before features are computed, while the quality gate reads
        # track_duration_s and skeleton_coverage from the PRE-filter skeletons
        # table — so such a worm can pass the gate and still have g empty.
        # Reporting 0.0 for its speeds, reversals and rate put un-measured
        # worms into the condition means as real zeros, a downward bias that is
        # worst in exactly the conditions where tracking is worst (i.e. it
        # manufactures a dose-response). aggregate_per_condition filters
        # non-finite values, so NaN keeps them out of the means while the row
        # stays visible in per_worm. Note fraction_forward/backward/paused
        # below already do this; these fields were the inconsistency.
        if n == 0:
            mean_forward_speed_pxs = float("nan")
            mean_backward_speed_pxs = float("nan")
            reversal_count = float("nan")
            reversal_rate_per_min = float("nan")

        rows.append({
            "condition": condition,
            "plate": plate,
            "video_name": video_name,
            "worm_index": gid,
            "repr_tierpsy_id": repr_id,
            "member_tierpsy_ids": ";".join(str(m) for m in member_tids),
            # Renderer-only (absent from PER_WORM_COLS): which frame window of
            # which Tierpsy fragment belongs to THIS track. Needed because a
            # fragment cut at a collision yields two tracks sharing one Tierpsy
            # id, so the renders cannot key on the id alone.
            "member_spans": [
                (frag_meta[m][0], frag_meta[m][1], frag_meta[m][2])
                for m in member_ids if m in frag_meta
            ],
            "group_classification": "linked",
            "bpm": bpm,
            "bend_interval_cv": bend_cv,
            "is_long": is_long,
            "mean_speed_pxs": mean_speed_pxs,
            "mean_forward_speed_pxs": mean_forward_speed_pxs,
            "mean_backward_speed_pxs": mean_backward_speed_pxs,
            "fraction_forward": (float(np.mean(speed > 0)) if n else float("nan")),
            "fraction_backward": (float(np.mean(speed < 0)) if n else float("nan")),
            "fraction_paused": (float(np.mean(abs_speed < paused_threshold))
                                if n else float("nan")),
            "reversal_count": reversal_count,
            "reversal_rate_per_min": reversal_rate_per_min,
            "path_length_px": path_length_px,
            "net_displacement_px": net_displacement_px,
            "tortuosity": tortuosity,
            "mean_length_px": mean_length_px,
            "mean_width_midbody_px": (_mean_finite(g["width_midbody"].values.astype(float))
                                      if "width_midbody" in g.columns else float("nan")),
            "track_duration_s": track_duration_s,
            "longest_continuous_run_s": longest_continuous_run_s,
            "skeleton_coverage": skeleton_coverage,
            "mean_speed_when_moving": mean_speed_when_moving,
            "activity_fraction_above_1pxs": activity_fraction_above_1pxs,
            "activity_fraction_above_3pxs": activity_fraction_above_3pxs,
            "activity_fraction_above_5pxs": activity_fraction_above_5pxs,
            "speed_cv": speed_cv,
            "length_cv": length_cv,
            "passed_filter": _passes_filter(
                track_duration_s, skeleton_coverage, min_span_s
            ),
            # Renderer-only (not spreadsheet columns; dropped by the PER_WORM_COLS
            # DataFrame projection). reversal_frames drives the path-traces flash;
            # arrow_* feed the velocity-arrow overlay + event markers. arrow_x/y/vx/vy
            # are dense per-frame arrays starting at frame arrow_f0.
            "reversal_frames": reversal_frames,
            "arrow_f0": arrow_f0,
            "arrow_x": arrow_x,
            "arrow_y": arrow_y,
            # arrow_vx/vy + arrow_*_event_frames + the arrow_* metric columns are
            # filled in the second pass (need plate_mean_length_px).
        })
        # Retain each worm's per-frame |speed| (aligned with rows) so the BL
        # activity fractions can be recomputed against the per-video scalar below.
        abs_speed_by_row.append(abs_speed)

    # --- Second pass: per-video body-length calibration ---------------------
    # One length scalar for the whole video (plate optical magnification), used as
    # the denominator for every BL column so individual worm-size variation is NOT
    # divided out. Computed from ALL grouped worms (not just kept ones) — see
    # _plate_mean_length. Repeated into every row as plate_mean_length_px.
    lengths = np.array([r["mean_length_px"] for r in rows], dtype=float)
    spans = np.array([r["track_duration_s"] for r in rows], dtype=float)
    plate_mean_length_px = _plate_mean_length(lengths, spans)
    for r, absx in zip(rows, abs_speed_by_row):
        r["plate_mean_length_px"] = plate_mean_length_px
        # Velocity-arrow reversal / turn events, now that the per-video plate scalar
        # is known (BL-relative speed cutoff). vx/vy come from the same call and do
        # not depend on the cutoff. Offsets are indices into the dense centroid
        # track; add arrow_f0 for absolute frame numbers (consumed by the renderer).
        av = _velocity_arrow_events(r["arrow_x"], r["arrow_y"], fps, plate_mean_length_px)
        af0 = r["arrow_f0"]
        r["arrow_reversal_count"] = av["reversal_count"]
        r["arrow_reversal_rate_per_min"] = av["reversal_rate_per_min"]
        r["turn_count"] = av["turn_count"]
        r["turn_rate_per_min"] = av["turn_rate_per_min"]
        r["arrow_vx"] = av["vx"]
        r["arrow_vy"] = av["vy"]
        if af0 is not None:
            r["arrow_reversal_event_frames"] = [af0 + o for o in av["reversal_offsets"]]
            r["arrow_turn_event_frames"] = [af0 + o for o in av["turn_offsets"]]
        else:
            r["arrow_reversal_event_frames"] = []
            r["arrow_turn_event_frames"] = []
        r["mean_speed_bls"] = _per_bodylength(r["mean_speed_pxs"], plate_mean_length_px)
        r["mean_forward_speed_bls"] = _per_bodylength(r["mean_forward_speed_pxs"], plate_mean_length_px)
        r["mean_backward_speed_bls"] = _per_bodylength(r["mean_backward_speed_pxs"], plate_mean_length_px)
        r["mean_speed_when_moving_bls"] = _per_bodylength(r["mean_speed_when_moving"], plate_mean_length_px)
        r["path_length_bl"] = _per_bodylength(r["path_length_px"], plate_mean_length_px)
        r["net_displacement_bl"] = _per_bodylength(r["net_displacement_px"], plate_mean_length_px)
        if len(absx) and np.isfinite(plate_mean_length_px) and plate_mean_length_px > 0:
            r["activity_fraction_above_0p05_bls"] = float(np.mean(absx >= 0.05 * plate_mean_length_px))
            r["activity_fraction_above_0p10_bls"] = float(np.mean(absx >= 0.10 * plate_mean_length_px))
            r["activity_fraction_above_0p20_bls"] = float(np.mean(absx >= 0.20 * plate_mean_length_px))
        else:
            r["activity_fraction_above_0p05_bls"] = float("nan")
            r["activity_fraction_above_0p10_bls"] = float("nan")
            r["activity_fraction_above_0p20_bls"] = float("nan")

    return rows


def aggregate_per_condition(per_worm_rows: list[dict],
                            min_span_s: float | None = None,
                            by_timepoint: bool = False) -> list[dict]:
    """
    Aggregate per-worm rows into one row per condition.

    Low-quality tracks (see _passes_filter / MIN_SPAN_S /
    SKELETON_COVERAGE_MIN) are dropped before computing metric aggregates.
    The filter is recomputed here from the raw track_duration_s /
    skeleton_coverage columns, so thresholds can be re-tuned on a saved
    per_worm table without re-running Tierpsy. min_span_s overrides the
    minimum-span threshold (must match the value used to build the
    per_worm passed_filter column); None falls back to MIN_SPAN_S.

    ``by_timepoint`` groups on (timepoint_h, condition) instead of condition
    alone, for a multi-folder timecourse.

    Each condition row reports n_worms_total (before filtering),
    n_worms_kept (after filtering), and mean / median / std of every AGG_COLS
    metric computed only on the kept worms, plus a median-only column for each
    ACTIVITY_COLS and BL_COLS metric.
    """
    import pandas as pd

    if not per_worm_rows:
        return []

    df = pd.DataFrame(per_worm_rows)
    out: list[dict] = []

    # In a timecourse a condition exists once per timepoint. Pooling across
    # timepoints would average away the change the timecourse is measuring, so
    # the group key gains the timepoint and every row carries it.
    if by_timepoint and "timepoint_h" in df.columns:
        tps = sorted({t for t in pd.to_numeric(df["timepoint_h"],
                                               errors="coerce").tolist()
                      if t == t})
        groups = [((tp, cond),
                   df[(pd.to_numeric(df["timepoint_h"], errors="coerce") == tp)
                      & (df["condition"].astype(str) == cond)])
                  for tp in tps
                  for cond in sorted(df["condition"].astype(str).unique())]
        groups = [(k, g) for k, g in groups if len(g)]
    else:
        groups = [((None, cond), df[df["condition"].astype(str) == cond])
                  for cond in sorted(df["condition"].astype(str).unique())]

    for (tp, cond), cdf in groups:
        passed_mask = cdf.apply(
            lambda r: _passes_filter(
                r.get("track_duration_s"), r.get("skeleton_coverage"),
                min_span_s,
            ),
            axis=1,
        )
        kept = cdf[passed_mask.values]
        agg: dict = {
            "condition": cond,
            "n_worms_total": int(len(cdf)),
            "n_worms_kept": int(len(kept)),
        }
        if tp is not None:
            agg = {"timepoint_h": tp, **agg}
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
        # Activity / variability + body-length-normalized + velocity-arrow
        # metrics: median only.
        for col in ACTIVITY_COLS + BL_COLS + ARROW_COLS:
            if col in kept.columns:
                vals = pd.to_numeric(kept[col], errors="coerce").values.astype(float)
                vals = vals[np.isfinite(vals)]
            else:
                vals = np.array([], dtype=float)
            agg[f"{col}_median"] = float(np.median(vals)) if len(vals) else None
        out.append(agg)
    return out
