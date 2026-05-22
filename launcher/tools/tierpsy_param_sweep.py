#!/usr/bin/env python3
"""
Tierpsy parameter sweep harness — Phase 1 / Phase 1b / Phase 1c / Phase 1d / Phase 2.

Runs Tierpsy on one or more test videos across a set of parameter variants and
emits per-run metrics + skeleton-overlay MP4 for each run.

Usage
-----
# Phase 1 (one-at-a-time sweep, all others at baseline):
python launcher/tools/tierpsy_param_sweep.py \\
    --video C:/path/to/video.mp4

# Phase 1b (focused sweep on top of Phase 1 winner filt_max_width_ratio=4):
python launcher/tools/tierpsy_param_sweep.py \\
    --video C:/path/to/video.mp4 --phase 1b \\
    [--base-params C:/path/to/motility_params.json] \\
    [--sweep-root C:/path/to/output/]

# Phase 1c (flicker hunt: max_gap_allowed_block, roi_size, w_head_angle_thresh):
python launcher/tools/tierpsy_param_sweep.py \\
    --video C:/path/to/video.mp4 --phase 1c \\
    [--base-params C:/path/to/motility_params.json] \\
    [--sweep-root C:/path/to/output/]

# Phase 1d (skeleton smoothing + sanity-check defaults run):
python launcher/tools/tierpsy_param_sweep.py \\
    --video C:/path/to/video.mp4 --phase 1d \\
    [--base-params C:/path/to/motility_params.json] \\
    [--sweep-root C:/path/to/output/]

# Phase 2 (skeleton stability sweep, multi-video, flicker+pipeline metrics):
python launcher/tools/tierpsy_param_sweep.py \\
    --video C:/path/to/video1.mp4 C:/path/to/video2.mp4 --phase 2 \\
    [--base-params C:/path/to/motility_params.json] \\
    [--sweep-root C:/path/to/output/]
"""
import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Allow importing sibling analysis/ and config modules when run as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from analysis.analysis_csv import bends_per_minute, compute_head_angle_signal
from analysis.ffmpeg_utils import convert_to_avi, probe_fps
from analysis.render_video import render_tracked

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Keys consumed by WormScan post-processing, not valid in Tierpsy's JSON.
_WORMSCAN_ONLY_KEYS: frozenset[str] = frozenset({"head_angle_prominence"})

_DEFAULT_PARAMS = Path(__file__).parent.parent / "motility_params.json"

# ---------------------------------------------------------------------------
# Sweep definitions
# ---------------------------------------------------------------------------

# Phase 1: one-at-a-time sweep, all others held at motility_params.json baseline.
SWEEP_PARAMS_1: list[tuple[str, list]] = [
    ("w_head_angle_thresh",   [60,    90,    120  ]),
    ("worm_bw_thresh_factor", [0.95,  1.05,  1.15 ]),
    ("filt_max_width_ratio",  [2.25,  4,     6    ]),
    ("filt_max_area_ratio",   [6,     15,    30   ]),
    ("filt_critical_alpha",   [0.001, 0.01,  0.1  ]),
    ("traj_area_ratio_lim",   [2,     4,     8    ]),
]

# Phase 1b: focused sweep layered on top of the Phase 1 winner.
# Every run (including baseline) inherits _PHASE_1B_BASE_OVERRIDES.
_PHASE_1B_BASE_OVERRIDES: dict = {"filt_max_width_ratio": 4}

SWEEP_PARAMS_1B: list[tuple[str, list]] = [
    ("feat_gap_to_interp_s", [0.25, 0.5, 1.0]),  # 0.25 = baseline, deduped out
    ("strel_size",           [3,    5,   7  ]),   # 5    = baseline, deduped out
]

# Phase 1c: flicker hunt — gap interpolation at skeletonisation level, ROI
# stability, and stricter head/tail detection.  No base overrides needed;
# motility_params.json already carries the Phase 1/1b winners as baseline.
SWEEP_PARAMS_1C: list[tuple[str, list]] = [
    ("max_gap_allowed_block", [-1, 10, 30, 60]),  # -1 = baseline, deduped out
    ("roi_size",              [-1, 256, 512  ]),  # -1 = baseline, deduped out
    ("w_head_angle_thresh",   [30, 45,  60  ]),   # 60 = baseline, deduped out
]

# Phase 1d: skeleton smoothing sweep + sanity-check run against Tierpsy defaults.
# Both smoothing params are already at their Tierpsy defaults in motility_params.json
# (feat_skel_smooth_window=5, feat_coords_smooth_window_s=0.25), so those values
# deduplicate to run_000_baseline.
SWEEP_PARAMS_1D: list[tuple[str, list]] = [
    ("feat_skel_smooth_window",      [5,    9,   15 ]),  # 5    = baseline, deduped out
    ("feat_coords_smooth_window_s",  [0.25, 0.5, 1.0]),  # 0.25 = baseline, deduped out
]

# Minimal overrides applied on top of Tierpsy's raw defaults for the sanity-check run.
# expected_fps is overwritten from the probed value in run_one, but kept here as intent.
# analysis_checkpoints must be listed explicitly: without them, Tierpsy's "BASE" type
# omits FEAT_TIERPSY and produces no _featuresN.hdf5 (root cause of Phase 1d crash).
_SANITY_OVERRIDES: dict = {
    "is_light_background": False,
    "use_nn_food_cnt": False,
    "nn_filter_to_use": "none",
    "analysis_type": "BASE",
    "analysis_checkpoints": [
        "COMPRESS", "TRAJ_CREATE", "TRAJ_JOIN", "SKE_INIT", "BLOB_FEATS",
        "SKE_CREATE", "SKE_FILT", "SKE_ORIENT", "INT_PROFILE", "INT_SKE_ORIENT",
        "FEAT_INIT", "FEAT_TIERPSY",
    ],
    "expected_fps": 30.0,
}

# Phase 2: skeleton stability — sweep params that affect skeleton-length consistency.
# skel_smooth_window is not in motility_params.json (Tierpsy default used), so all 3
# values will produce unique runs.  The other three deduplicate at their baseline values.
SWEEP_PARAMS_2: list[tuple[str, list]] = [
    ("skel_smooth_window",           [3,   5,   9  ]),  # Tierpsy default ~5; not in our baseline
    ("feat_skel_smooth_window",      [3,   5,  11  ]),  # 5  = baseline in motility_params.json
    ("feat_coords_smooth_window_s",  [0.1, 0.25, 0.5]), # 0.25 = baseline
    ("w_head_angle_thresh",          [30,  60,  90  ]),  # 60  = baseline
]

# Extra metric columns computed by running the full WormScan pipeline (Phase 2 only).
PHASE2_EXTRA_METRIC_COLS: list[str] = [
    "total_flicker_frames",
    "tracks_with_any_flicker",
    "tracks_with_any_flicker_frac",
    "pipeline_surviving_tracks",
    "bpm_mean_surviving",
    "bpm_median_surviving",
]

METRIC_COLS: list[str] = [
    "total_fragments",
    "mean_fragment_duration_s",
    "median_fragment_duration_s",
    "mean_valid_skeleton_fraction",
    "fragments_with_5s_continuous",
    "fragments_with_10s_continuous",
    "fragments_with_15s_continuous",
    "total_seconds_in_10s_runs",
    "bpm_mean",
    "bpm_median",
    "bpm_std",
]

# ---------------------------------------------------------------------------
# Run plan
# ---------------------------------------------------------------------------

def _values_equal(a, b) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return a == b


def _fmt_val(v) -> str:
    """Format a value for use in a directory name (dots → 'p')."""
    return str(v).replace(".", "p")


def build_run_plan(
    base_params: dict,
    sweep_params: list[tuple[str, list]],
    base_overrides: dict | None = None,
) -> list[dict]:
    """
    Return the deduplicated list of runs. run_000_baseline anchors all sweeps.

    base_overrides (e.g. Phase 1b's {"filt_max_width_ratio": 4}) are applied
    to base_params before building every run, including the baseline.  A sweep
    variant whose value equals the resulting effective baseline is skipped —
    its result is already captured by run_000.
    """
    effective_base = {**base_params, **(base_overrides or {})}
    runs: list[dict] = [
        {
            "run_id": "run_000_baseline",
            "param_changed": "baseline",
            "param_value": None,
            "params": effective_base,
        }
    ]
    counter = 1
    for param_name, values in sweep_params:
        baseline_val = effective_base.get(param_name)
        for v in values:
            if _values_equal(v, baseline_val):
                continue
            runs.append({
                "run_id": f"run_{counter:03d}_{param_name}_{_fmt_val(v)}",
                "param_changed": param_name,
                "param_value": v,
                "params": {**effective_base, param_name: v},
            })
            counter += 1
    return runs


# ---------------------------------------------------------------------------
# Tierpsy invocation — custom two-path layout for the sweep
# ---------------------------------------------------------------------------

def _run_tierpsy_sweep(
    sweep_root: Path,
    avi_name: str,
    run_name: str,
    tierpsy_image: str,
    docker_cmd: str = "docker",
    timeout_s: int = 600,
) -> tuple[str, str]:
    """
    Run Tierpsy for one sweep run.

    Mounts sweep_root as /sweep so the shared AVI (at /sweep/<avi_name>)
    and the per-run output directories (at /sweep/<run_name>/) are both
    visible inside the container.  Returns (stdout, stderr).
    Raises RuntimeError on non-zero exit or timeout.
    """
    sweep_posix = sweep_root.as_posix()
    cmd = [
        docker_cmd, "run", "--rm",
        "-v", f"{sweep_posix}:/sweep",
        tierpsy_image,
        "tierpsy_process",
        "--video_dir_root",   "/sweep",
        "--mask_dir_root",    f"/sweep/{run_name}/MaskedVideos",
        "--results_dir_root", f"/sweep/{run_name}/Results",
        "--pattern_include",  avi_name,
        "--json_file",        f"/sweep/{run_name}/params.json",
        "--max_num_process",  "1",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Tierpsy timed out after {timeout_s}s")
    if result.returncode != 0:
        raise RuntimeError(
            f"Tierpsy exited {result.returncode}:\n{result.stderr.strip()[-1000:]}"
        )
    return result.stdout, result.stderr


def _tierpsy_default_params(tierpsy_image: str, docker_cmd: str) -> dict:
    """Query the Tierpsy container for its built-in default parameter dict."""
    script = (
        "from tierpsy.helper.params import TrackerParams; "
        "import json; "
        "print(json.dumps(TrackerParams().p_dict))"
    )
    result = subprocess.run(
        [docker_cmd, "run", "--rm", tierpsy_image, "python3", "-c", script],
        capture_output=True, text=True, timeout=60,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to query Tierpsy defaults:\n{result.stderr.strip()[-500:]}"
        )
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def compute_validity(traj_data_rows) -> np.ndarray:
    """Return a boolean array of per-frame skeleton validity.

    A frame is considered valid only if Tierpsy itself marked it as skeletonized
    via the `was_skeletonized` flag in trajectories_data. This matches what the
    skeleton overlay renderer draws.
    """
    return np.asarray(traj_data_rows["was_skeletonized"]) == 1


def _consecutive_valid_runs(frame_nums: np.ndarray, valid: np.ndarray) -> list[int]:
    """
    Return lengths of runs of consecutive-frame-number valid-skeleton frames.
    A run breaks on a gap in frame numbering or a False valid entry.
    """
    runs: list[int] = []
    n = len(frame_nums)
    i = 0
    while i < n:
        if not valid[i]:
            i += 1
            continue
        j = i + 1
        while j < n and valid[j] and int(frame_nums[j]) - int(frame_nums[j - 1]) == 1:
            j += 1
        runs.append(j - i)
        i = j
    return runs


def _nan_metrics(error: str) -> dict:
    nan = float("nan")
    return {col: nan for col in METRIC_COLS} | {"error": error}


def compute_metrics(featuresN_path: Path, fps: float, head_angle_prominence: float) -> dict:
    """Compute all sweep metrics from a _featuresN.hdf5 file."""
    import h5py
    import pandas as pd

    nan = float("nan")

    try:
        traj = pd.read_hdf(str(featuresN_path), key="trajectories_data")
        with h5py.File(str(featuresN_path), "r") as fh:
            skel_all: np.ndarray = fh["coordinates"]["skeletons"][:]
    except Exception as exc:
        return _nan_metrics(f"hdf5_read_failed: {exc}")

    frame_col = "timestamp_raw" if "timestamp_raw" in traj.columns else "frame_number"
    total_fragments = int(traj["worm_index_joined"].nunique())

    frag_durations: list[float] = []
    frag_valid_fracs: list[float] = []
    frags_5s = frags_10s = frags_15s = 0
    total_seconds_10s = 0.0
    bpms: list[float] = []

    for _wi, grp in traj.groupby("worm_index_joined"):
        grp = grp.sort_values(frame_col).reset_index(drop=True)
        raw_fns = grp[frame_col].values

        valid = compute_validity(grp)

        frag_durations.append(len(grp) / fps)
        frag_valid_fracs.append(float(valid.mean()) if len(valid) else 0.0)

        # Frame-number-consecutive runs of valid skeletons.
        try:
            fns_int = np.array([int(float(f)) for f in raw_fns], dtype=np.int64)
        except (ValueError, TypeError):
            fns_int = np.arange(len(raw_fns), dtype=np.int64)
        run_lengths = _consecutive_valid_runs(fns_int, valid)
        run_secs = [r / fps for r in run_lengths]

        if any(s >= 5.0 for s in run_secs):
            frags_5s += 1
        if any(s >= 10.0 for s in run_secs):
            frags_10s += 1
        if any(s >= 15.0 for s in run_secs):
            frags_15s += 1
        total_seconds_10s += sum(s for s in run_secs if s >= 10.0)

        # BPM for fragments with ≥5 s of valid-skeleton data.
        if valid.sum() / fps >= 5.0:
            signal = compute_head_angle_signal(grp, skel_all, fps, head_angle_prominence)
            if signal is not None:
                bpms.append(bends_per_minute(signal, fps))

    return {
        "total_fragments": total_fragments,
        "mean_fragment_duration_s": float(np.mean(frag_durations)) if frag_durations else nan,
        "median_fragment_duration_s": float(np.median(frag_durations)) if frag_durations else nan,
        "mean_valid_skeleton_fraction": float(np.mean(frag_valid_fracs)) if frag_valid_fracs else nan,
        "fragments_with_5s_continuous": frags_5s,
        "fragments_with_10s_continuous": frags_10s,
        "fragments_with_15s_continuous": frags_15s,
        "total_seconds_in_10s_runs": round(total_seconds_10s, 2),
        "bpm_mean": float(np.mean(bpms)) if bpms else nan,
        "bpm_median": float(np.median(bpms)) if bpms else nan,
        "bpm_std": float(np.std(bpms)) if bpms else nan,
        "error": None,
    }


def compute_pipeline_metrics(
    featuresN_path: Path,
    fps: float,
    head_angle_prominence: float,
) -> dict:
    """
    Run the full WormScan fragment-grouping pipeline and return flicker + survival metrics.
    Used for Phase 2 comparison rows.
    """
    from analysis.analysis_csv import read_fragments

    nan = float("nan")
    try:
        rows, analysis_log = read_fragments(
            featuresN_path, fps,
            condition="sweep", plate=featuresN_path.stem,
            head_angle_prominence=head_angle_prominence,
        )
    except Exception as exc:
        return {col: nan for col in PHASE2_EXTRA_METRIC_COLS} | {"pipeline_error": str(exc)[:200]}

    flicker_stats = analysis_log.get("flicker_stats", {})
    total_flicker = flicker_stats.get("total_flicker_frames", 0)
    tracks_with_flicker = flicker_stats.get("tracks_with_any_flicker", 0)
    input_tracks = max(analysis_log.get("input_track_count", 1), 1)
    bpms = [r["bpm"] for r in rows]

    return {
        "total_flicker_frames": total_flicker,
        "tracks_with_any_flicker": tracks_with_flicker,
        "tracks_with_any_flicker_frac": round(tracks_with_flicker / input_tracks, 4),
        "pipeline_surviving_tracks": len(rows),
        "bpm_mean_surviving": float(np.mean(bpms)) if bpms else nan,
        "bpm_median_surviving": float(np.median(bpms)) if bpms else nan,
        "pipeline_error": None,
    }


# ---------------------------------------------------------------------------
# Per-run orchestration
# ---------------------------------------------------------------------------

def run_one(
    run: dict,
    sweep_root: Path,
    shared_avi: Path,
    fps: float,
    head_angle_prominence: float,
    tierpsy_image: str,
    docker_cmd: str,
    timeout_s: int,
    compute_extra_metrics: bool = False,
) -> dict:
    """Execute one sweep run. Returns a flat row dict for comparison.csv."""
    run_id: str = run["run_id"]
    run_dir = sweep_root / run_id
    run_dir.mkdir(exist_ok=True)
    (run_dir / "MaskedVideos").mkdir(exist_ok=True)
    (run_dir / "Results").mkdir(exist_ok=True)

    # Write params JSON: strip WormScan-only keys, patch fps.
    tierpsy_params = {k: v for k, v in run["params"].items()
                      if k not in _WORMSCAN_ONLY_KEYS}
    tierpsy_params["expected_fps"] = fps
    (run_dir / "params.json").write_text(
        json.dumps(tierpsy_params, indent=2), encoding="utf-8"
    )

    log_path = run_dir / "tierpsy.log"

    # Run Tierpsy.
    error_msg: str | None = None
    try:
        print("    running Tierpsy…", flush=True)
        stdout, stderr = _run_tierpsy_sweep(
            sweep_root=sweep_root,
            avi_name=shared_avi.name,
            run_name=run_id,
            tierpsy_image=tierpsy_image,
            docker_cmd=docker_cmd,
            timeout_s=timeout_s,
        )
        log_path.write_text(stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8")
    except RuntimeError as exc:
        error_msg = str(exc)
        log.warning("%s: Tierpsy failed: %s", run_id, error_msg[:200])
        log_path.write_text(str(exc), encoding="utf-8")

    # Locate outputs.
    stem = shared_avi.stem
    results_dir = run_dir / "Results"
    featuresN = results_dir / f"{stem}_featuresN.hdf5"
    skeletons_hdf5 = results_dir / f"{stem}_skeletons.hdf5"

    # Metrics.
    if featuresN.exists():
        print("    computing metrics…", flush=True)
        metrics = compute_metrics(featuresN, fps, head_angle_prominence)
        if compute_extra_metrics:
            print("    computing pipeline metrics…", flush=True)
            extra = compute_pipeline_metrics(featuresN, fps, head_angle_prominence)
            metrics.update(extra)
    else:
        err = error_msg or "no _featuresN.hdf5 produced"
        metrics = _nan_metrics(err)
        if compute_extra_metrics:
            metrics.update({col: float("nan") for col in PHASE2_EXTRA_METRIC_COLS})
            metrics["pipeline_error"] = err
        log.warning("%s: %s", run_id, err)

    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8"
    )

    # Skeleton overlay — reuse render_tracked (skeleton polylines, color by fragment ID).
    if skeletons_hdf5.exists():
        print("    rendering skeleton overlay…", flush=True)
        try:
            render_tracked(
                shared_avi,
                skeletons_hdf5,
                run_dir / "skeleton_overlay.mp4",
                fps,
            )
        except Exception as exc:
            log.warning("%s: render_tracked failed: %s", run_id, exc)

    row = {
        "run_id": run_id,
        "param_changed": run["param_changed"],
        "param_value": run["param_value"],
    }
    row.update(metrics)
    return row


# ---------------------------------------------------------------------------
# Output: comparison.csv
# ---------------------------------------------------------------------------

def write_comparison_csv(
    rows: list[dict],
    path: Path,
    extra_cols: "list[str] | None" = None,
    include_video: bool = False,
) -> None:
    import csv
    if not rows:
        return
    prefix = ["video"] if include_video else []
    fieldnames = (prefix + ["run_id", "param_changed", "param_value"]
                  + METRIC_COLS + (extra_cols or []) + ["error"])
    if include_video and extra_cols:
        # pipeline_error travels alongside the phase2 extra cols
        fieldnames.append("pipeline_error")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Output: summary.png
# ---------------------------------------------------------------------------

def write_summary_png(
    rows: list[dict],
    base_params: dict,
    sweep_params: list[tuple[str, list]],
    phase: str,
    path: Path,
) -> None:
    """
    One subplot per sweep parameter.  X = parameter value, Y = headline metric
    (total_seconds_in_10s_runs).  Baseline value is marked with a navy diamond;
    other values with steelblue circles.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    baseline_row = next((r for r in rows if r["param_changed"] == "baseline"), None)
    baseline_y = (
        baseline_row.get("total_seconds_in_10s_runs", float("nan"))
        if baseline_row else float("nan")
    )

    ncols = 3
    nrows = (len(sweep_params) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(ncols * 4, nrows * 3.5), squeeze=False)
    axes_flat = [axes[r][c] for r in range(nrows) for c in range(ncols)]

    for idx, (param_name, values) in enumerate(sweep_params):
        ax = axes_flat[idx]
        baseline_x = base_params.get(param_name)

        xs: list[float] = []
        ys: list[float] = []
        is_base: list[bool] = []

        for v in values:
            if _values_equal(v, baseline_x):
                xs.append(float(v))
                ys.append(baseline_y)
                is_base.append(True)
            else:
                match = next(
                    (r for r in rows
                     if r["param_changed"] == param_name
                     and _values_equal(r.get("param_value"), v)),
                    None,
                )
                xs.append(float(v))
                ys.append(
                    match.get("total_seconds_in_10s_runs", float("nan"))
                    if match else float("nan")
                )
                is_base.append(False)

        order = sorted(range(len(xs)), key=lambda i: xs[i])
        xs = [xs[i] for i in order]
        ys = [ys[i] for i in order]
        is_base = [is_base[i] for i in order]

        ax.plot(xs, ys, color="steelblue", linewidth=1, alpha=0.6)
        for x, y, base in zip(xs, ys, is_base):
            ax.scatter([x], [y],
                       color="navy" if base else "steelblue",
                       marker="D" if base else "o",
                       s=60, zorder=5)

        ax.set_title(param_name, fontsize=9)
        ax.set_xlabel("value", fontsize=8)
        ax.set_ylabel("total_s_in_10s_runs", fontsize=7)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)

    for idx in range(len(sweep_params), len(axes_flat)):
        axes_flat[idx].set_visible(False)

    fig.suptitle(
        f"Phase {phase} sweep — total_seconds_in_10s_runs\n(navy ◆ = baseline value)",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    log.info("Summary PNG written: %s", path)


# ---------------------------------------------------------------------------
# Output: README.md
# ---------------------------------------------------------------------------

def write_readme(
    sweep_root: Path,
    video_path: Path,
    base_params_path: Path,
    runs: list[dict],
    timestamp: str,
    phase: str,
    base_overrides: dict | None = None,
) -> None:
    lines = [
        f"# Tierpsy Parameter Sweep — Phase {phase}",
        "",
        f"Generated: {timestamp}",
        f"Video: `{video_path}`",
        f"Base params: `{base_params_path}`",
    ]
    if base_overrides:
        overrides_str = ", ".join(f"`{k}={v}`" for k, v in base_overrides.items())
        lines += [
            f"Base overrides (applied to every run): {overrides_str}",
        ]
    lines += [
        "",
        "## Runs",
        "",
        "| Run | Param changed | Value |",
        "|-----|--------------|-------|",
    ]
    for r in runs:
        val = r["param_value"] if r["param_value"] is not None else "*(all baseline)*"
        lines.append(f"| `{r['run_id']}` | `{r['param_changed']}` | {val} |")
    lines += [
        "",
        "## Files per run",
        "",
        "- `params.json` — Tierpsy config used",
        "- `MaskedVideos/` — Tierpsy masked-video output",
        "- `Results/` — Tierpsy results (HDF5 files)",
        "- `skeleton_overlay.mp4` — original frames with skeleton polylines, coloured by worm ID",
        "- `metrics.json` — per-run metrics",
        "- `tierpsy.log` — Tierpsy stdout / stderr",
        "",
        "## Headline metric",
        "",
        "`total_seconds_in_10s_runs` — sum across all fragments of the total time spent in"
        " any run of ≥10 s of continuous valid skeleton. Higher = better coverage for"
        " trustworthy bend measurement.",
        "",
        "## Summary files",
        "",
        "- `comparison.csv` — all metrics in one table",
        "- `summary.png` — headline metric vs parameter value, one subplot per parameter",
        "",
    ]
    (sweep_root / "README.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", required=True, nargs="+",
                        help="Path to one or more test MP4s "
                             "(phase 2 accepts multiple: --video v1.mp4 v2.mp4)")
    parser.add_argument("--base-params", default=str(_DEFAULT_PARAMS),
                        help="Path to motility_params.json "
                             f"(default: {_DEFAULT_PARAMS})")
    parser.add_argument("--sweep-root", default=None,
                        help="Output directory "
                             "(default: <video_dir>/sweep_phase<N>_<UTC_timestamp>/)")
    parser.add_argument("--phase", default="1", choices=["1", "1b", "1c", "1d", "2"],
                        help="Sweep phase: '1' (default), '1b', '1c', '1d', or '2'")
    args = parser.parse_args()

    phase = args.phase

    video_paths = [Path(v).resolve() for v in args.video]
    for vp in video_paths:
        if not vp.exists():
            sys.exit(f"Video not found: {vp}")
    # For single-video phases use the first (and only expected) path.
    video_path = video_paths[0]

    base_params_path = Path(args.base_params).resolve()
    if not base_params_path.exists():
        sys.exit(f"Base params not found: {base_params_path}")

    # Phase-specific sweep config.
    if phase == "1b":
        sweep_params = SWEEP_PARAMS_1B
        base_overrides: dict | None = _PHASE_1B_BASE_OVERRIDES
    elif phase == "1c":
        sweep_params = SWEEP_PARAMS_1C
        base_overrides = None
    elif phase == "1d":
        sweep_params = SWEEP_PARAMS_1D
        base_overrides = None
    elif phase == "2":
        sweep_params = SWEEP_PARAMS_2
        base_overrides = None
    else:
        sweep_params = SWEEP_PARAMS_1
        base_overrides = None

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    sweep_root = (
        Path(args.sweep_root).resolve()
        if args.sweep_root
        else video_path.parent / f"sweep_phase{phase}_{timestamp}"
    )
    sweep_root.mkdir(parents=True, exist_ok=True)

    print(f"Phase:       {phase}")
    print(f"Sweep root:  {sweep_root}")
    if phase == "2":
        for vp in video_paths:
            print(f"Video:       {vp}")
    else:
        print(f"Video:       {video_path}")
    print(f"Base params: {base_params_path}")
    if base_overrides:
        print(f"Overrides:   {base_overrides}")

    base_params = json.loads(base_params_path.read_text(encoding="utf-8"))
    head_angle_prominence = float(base_params.get("head_angle_prominence", 0.30))

    # Load Docker / image settings from WormScan config; fall back to defaults.
    try:
        from config import load as _load_config
        _s = _load_config()
        tierpsy_image = f"{_s.tierpsy_image}:{_s.tierpsy_image_tag}"
        docker_cmd = _s.docker_command
        timeout_s = _s.analysis_video_timeout_s
    except Exception:
        tierpsy_image = "tierpsy/tierpsy-tracker:latest"
        docker_cmd = "docker"
        timeout_s = 600

    print(f"Tierpsy image: {tierpsy_image}")

    # Build run plan (shared across all videos for phase 2).
    runs = build_run_plan(base_params, sweep_params, base_overrides)

    # Phase 1d and Phase 2: append sanity-check run using Tierpsy bare defaults.
    if phase in ("1d", "2"):
        print("\nQuerying Tierpsy defaults for sanity-check run…", flush=True)
        tierpsy_defaults = _tierpsy_default_params(tierpsy_image, docker_cmd)
        runs.append({
            "run_id": f"run_{len(runs):03d}_sanity_check_defaults",
            "param_changed": "sanity_check_defaults",
            "param_value": None,
            "params": {**tierpsy_defaults, **_SANITY_OVERRIDES},
        })

    print(f"\n{len(runs)} runs planned (baseline + {len(runs) - 1} variants):")
    for r in runs:
        val_str = str(r["param_value"]) if r["param_value"] is not None else "baseline"
        print(f"  {r['run_id']}  ({r['param_changed']} = {val_str})")

    # ---------------------------------------------------------------------------
    # Phase 2: multi-video execution
    # ---------------------------------------------------------------------------
    if phase == "2":
        all_rows: list[dict] = []
        for video_path in video_paths:
            video_stem = video_path.stem
            video_sweep_root = sweep_root / video_stem
            video_sweep_root.mkdir(exist_ok=True)

            print(f"\nProbing FPS for {video_path.name}…")
            fps = probe_fps(video_path)
            print(f"  fps = {fps:.3f}")

            shared_avi = video_sweep_root / (video_stem + ".avi")
            print(f"Converting to MJPEG AVI → {shared_avi.name}…")
            convert_to_avi(video_path, shared_avi)
            print("  AVI ready.")

            write_readme(video_sweep_root, video_path, base_params_path, runs, timestamp,
                         phase=phase, base_overrides=base_overrides)

            print(f"\nRunning {len(runs)} Tierpsy jobs for {video_stem}…\n")
            for i, run in enumerate(runs):
                print(f"[{i + 1}/{len(runs)}] {run['run_id']}  (video: {video_stem})", flush=True)
                row = run_one(
                    run=run,
                    sweep_root=video_sweep_root,
                    shared_avi=shared_avi,
                    fps=fps,
                    head_angle_prominence=head_angle_prominence,
                    tierpsy_image=tierpsy_image,
                    docker_cmd=docker_cmd,
                    timeout_s=timeout_s,
                    compute_extra_metrics=True,
                )
                row["video"] = video_stem
                all_rows.append(row)
                headline = row.get("total_seconds_in_10s_runs", float("nan"))
                err = row.get("error")
                status = f"error: {err}" if err else f"total_s_in_10s_runs = {headline:.1f}"
                print(f"    → {status}\n", flush=True)

        csv_path = sweep_root / "comparison.csv"
        write_comparison_csv(all_rows, csv_path,
                             extra_cols=PHASE2_EXTRA_METRIC_COLS,
                             include_video=True)
        print(f"comparison.csv  → {csv_path}")
        print(f"\nDone. Sweep results: {sweep_root}")
        return

    # ---------------------------------------------------------------------------
    # Single-video execution (phases 1, 1b, 1c, 1d)
    # ---------------------------------------------------------------------------

    # Probe FPS.
    print("\nProbing FPS…")
    fps = probe_fps(video_path)
    print(f"  fps = {fps:.3f}")

    # Convert MP4 → MJPEG AVI once; all runs share this file.
    shared_avi = sweep_root / (video_path.stem + ".avi")
    print(f"\nConverting to MJPEG AVI → {shared_avi.name}…")
    convert_to_avi(video_path, shared_avi)
    print("  AVI ready.")

    write_readme(sweep_root, video_path, base_params_path, runs, timestamp,
                 phase=phase, base_overrides=base_overrides)

    # Execute all runs sequentially.
    print(f"\nRunning {len(runs)} Tierpsy jobs…\n")
    single_rows: list[dict] = []
    for i, run in enumerate(runs):
        print(f"[{i + 1}/{len(runs)}] {run['run_id']}", flush=True)
        row = run_one(
            run=run,
            sweep_root=sweep_root,
            shared_avi=shared_avi,
            fps=fps,
            head_angle_prominence=head_angle_prominence,
            tierpsy_image=tierpsy_image,
            docker_cmd=docker_cmd,
            timeout_s=timeout_s,
        )
        single_rows.append(row)
        headline = row.get("total_seconds_in_10s_runs", float("nan"))
        err = row.get("error")
        status = f"error: {err}" if err else f"total_s_in_10s_runs = {headline:.1f}"
        print(f"    → {status}\n", flush=True)

    # Write aggregate outputs.
    csv_path = sweep_root / "comparison.csv"
    write_comparison_csv(single_rows, csv_path)
    print(f"comparison.csv  → {csv_path}")

    png_path = sweep_root / "summary.png"
    write_summary_png(single_rows, base_params, sweep_params, phase, png_path)
    print(f"summary.png     → {png_path}")

    print(f"\nDone. Sweep results: {sweep_root}")


if __name__ == "__main__":
    main()
