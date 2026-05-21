"""
Step 3 of the motility pipeline: per-track skeleton-length flicker filter.

Skeleton length is computed from /coordinates/skeletons. Frames where the rolling
std of skeleton length exceeds the threshold (or where the skeleton is missing) are
flagged as flicker. The track is then split into clean contiguous sub-tracks at those
boundaries.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pandas as pd


def _skeleton_lengths(skel_all: np.ndarray, skeleton_ids: np.ndarray) -> np.ndarray:
    """Per-frame skeleton length; NaN for missing/invalid skeletons."""
    lengths = np.full(len(skeleton_ids), np.nan)
    for i, sid_raw in enumerate(skeleton_ids):
        if not np.isfinite(float(sid_raw)):
            continue
        sid = int(sid_raw)
        if sid < 0 or sid >= len(skel_all):
            continue
        skel = skel_all[sid]  # (49, 2)
        if not np.isfinite(skel).all():
            continue
        diffs = np.diff(skel, axis=0)
        lengths[i] = float(np.sum(np.sqrt((diffs ** 2).sum(axis=1))))
    return lengths


def _flicker_runs(mask: np.ndarray) -> int:
    """Count contiguous True runs in a boolean array."""
    count = 0
    in_run = False
    for v in mask:
        if v and not in_run:
            count += 1
            in_run = True
        elif not v:
            in_run = False
    return count


def _clean_spans(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Contiguous False (clean) spans as (start, end_exclusive) pairs."""
    spans: List[Tuple[int, int]] = []
    in_clean = False
    start = 0
    for i, is_flicker in enumerate(mask):
        if not is_flicker and not in_clean:
            start = i
            in_clean = True
        elif is_flicker and in_clean:
            spans.append((start, i))
            in_clean = False
    if in_clean:
        spans.append((start, len(mask)))
    return spans


def filter_track(
    track_df: pd.DataFrame,
    skel_all: np.ndarray,
    fps: float,
    window_s: float,
    std_threshold_px: float,
) -> dict:
    """
    Apply the flicker filter to a single track (must be sorted by frame number).

    Returns a dict with:
        clean_subtracks        — list of pd.DataFrame, each a contiguous clean segment
        flicker_frame_count    — total frames flagged as flicker
        flicker_stretch_count  — number of distinct flicker gaps
        longest_clean_s        — duration of the longest clean sub-track in seconds
        total_frames           — total frames in the input track
    """
    skeleton_ids = track_df["skeleton_id"].values
    lengths = _skeleton_lengths(skel_all, skeleton_ids)

    # Rolling std; odd window >= 3 frames
    window = max(3, int(fps * window_s) | 1)
    rolling_std = (
        pd.Series(lengths)
        .rolling(window, center=True, min_periods=1)
        .std(ddof=0)
        .fillna(0.0)
        .values
    )

    mask = np.isnan(lengths) | (rolling_std > std_threshold_px)

    spans = _clean_spans(mask)
    clean_subtracks = [track_df.iloc[s:e].reset_index(drop=True) for s, e in spans]
    clean_subtracks = [df for df in clean_subtracks if len(df) > 0]

    longest_clean_s = max((len(df) / fps for df in clean_subtracks), default=0.0)

    return {
        "clean_subtracks": clean_subtracks,
        "flicker_frame_count": int(np.sum(mask)),
        "flicker_stretch_count": _flicker_runs(mask),
        "longest_clean_s": longest_clean_s,
        "total_frames": len(track_df),
    }
