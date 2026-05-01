import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def bends_per_minute(group: pd.DataFrame, fps: float) -> "float | None":
    """
    Compute bends per minute for a single Tierpsy trajectory fragment.

    Counts sign changes of the curvature signal AFTER subtracting a slow
    rolling mean. This is robust to worms whose curvature is biased to one
    side (so the raw signal never crosses zero) — every real bend crosses
    the worm's own running midline, regardless of overall offset.

    Returns None if the fragment is too short (<10 valid samples).
    """
    sig = group["curvature_midbody"].dropna().values
    if len(sig) < 10:
        return None

    # Smooth slightly to suppress sample-to-sample noise (~0.3 s window)
    smooth_win = max(3, int(fps * 0.3) | 1)
    smoothed = pd.Series(sig).rolling(smooth_win, center=True, min_periods=1).mean().values

    # Detrend with a slow rolling mean (~2 s window) to remove low-frequency
    # offset/drift. Each remaining oscillation crosses the new zero line.
    detrend_win = max(5, int(fps * 2.0) | 1)
    baseline = pd.Series(smoothed).rolling(detrend_win, center=True, min_periods=1).mean().values
    detrended = smoothed - baseline

    crossings = int(np.sum(np.diff(np.sign(detrended)) != 0))
    bends = crossings / 2
    duration_min = len(detrended) / fps / 60
    return bends / duration_min


def read_fragments(
    hdf5_path: Path,
    fps: float,
    condition: str,
    plate: str,
    long_threshold_s: float = 5.0,
) -> list[dict]:
    """Read timeseries_data from a _featuresN.hdf5 and return per-fragment rows."""
    try:
        df = pd.read_hdf(str(hdf5_path), key="timeseries_data")
    except Exception as exc:
        log.error("Failed to read %s: %s", hdf5_path, exc)
        return []

    rows: list[dict] = []
    for worm_index, group in df.groupby("worm_index"):
        bpm = bends_per_minute(group, fps)
        if bpm is None:
            continue
        frames = len(group)
        duration_s = frames / fps
        rows.append(
            {
                "condition": condition,
                "plate": plate,
                "worm_index": int(worm_index),
                "frames": frames,
                "duration_s": round(duration_s, 3),
                "bpm": round(bpm, 2),
                "is_long": duration_s >= long_threshold_s,
                "fps_used": fps,
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
    }
