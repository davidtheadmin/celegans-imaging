"""
Position-based fragment linker for the crawling pipeline.

Crawling worms are slow crawlers that Tierpsy repeatedly loses (brief ~1s
segmentation failures during turns), shattering one worm's track into many short
worm_index_joined fragments. The shared motility grouping engine
(analysis.fragment_grouping.group_fragments — curl/collision spatiotemporal
clustering + flicker filter) under-counts these: on a validated 90s test clip
with ~10 visible worms it kept only ~7 as >=30s tracks, the rest dissolving into
rubble fragments.

This linker is a simpler, position-only nearest-neighbour stitcher with a
temporal-window ambiguity check. On the same clip it recovered 8 worms >=60s and
9 >=30s (45 fragments -> 11 groups), matching the human count.

Algorithm — for each fragment A (a worm_index_joined), processed in order of
f_end ascending:
  1. Candidate successors B are fragments that START in (A.f_end, A.f_end +
     T_MAX_S*fps], not yet claimed, not already in A's group.
  2. Score B by Euclidean distance from A's end position (mean of last 3 frames)
     to B's start position (mean of first 3 frames). Keep candidates within D_MAX.
  3. The nearest candidate wins, UNLESS it is ambiguous: among candidates that
     start within AMBIG_TIME_WINDOW_S*fps of the best one, if any has distance
     < AMBIG_RATIO * best_distance the link is refused (those fragments stay
     broken). Exception — noise floor: if both the best and the runner-up are
     < AMBIG_FLOOR_PX apart, the ambiguity check is skipped (sub-pixel ties are
     Tierpsy ghosts, not real worm collisions).
  4. Greedy + union-find: once B is claimed it leaves the candidate pool.

This is POSITION-ONLY — it does not attempt collision resolution. When a link is
genuinely ambiguous (a real crossing) the ambiguity rule refuses it; that is the
intended behaviour, not a gap to fill. Unlike fragment_grouping.py it neither
classifies groups (curl/collision) nor flicker-filters; crawling layers its own
kinematics + quality gate on top of the raw groups (see crawling_metrics.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Validated against the trimmed 90s test clip
# (_trimmed_..._skeletons.hdf5: 45 fragments -> 11 groups, 0 ambiguity skips).
D_MAX: float = 150.0            # px  — max end->start distance to link two fragments
T_MAX_S: float = 5.0            # s   — max temporal gap to search for a successor
AMBIG_RATIO: float = 2.0        # refuse if a competitor is < RATIO x the best distance
AMBIG_FLOOR_PX: float = 3.0     # ...unless both are sub-pixel (Tierpsy ghosts)
AMBIG_TIME_WINDOW_S: float = 1.0  # competitors must start within this of the best


def link_fragments(
    trajectories_data: pd.DataFrame,
    fps: float,
) -> "tuple[dict[int, list[int]], int]":
    """
    Group Tierpsy fragments into worm identities by position-based linking.

    trajectories_data — DataFrame from a *_skeletons.hdf5 trajectories_data table;
                        must have worm_index_joined, frame_number, coord_x, coord_y.
    fps               — frames per second (drives the temporal windows).

    Returns (groups, ambiguity_skips) where
        groups          — {group_id: [worm_index_joined, ...]} (group_id is the
                          union-find root, an arbitrary but stable member id).
        ambiguity_skips — number of links refused by the ambiguity rule.

    Returns ({}, 0) if the table is empty or lacks required columns.
    """
    required = {"worm_index_joined", "frame_number", "coord_x", "coord_y"}
    if trajectories_data is None or len(trajectories_data) == 0:
        return {}, 0
    if not required.issubset(trajectories_data.columns):
        return {}, 0

    # --- one fragment record per worm_index_joined ---
    frags: list[dict] = []
    for wid, sub in trajectories_data.groupby("worm_index_joined"):
        sub = sub.sort_values("frame_number")
        f0 = int(sub["frame_number"].iloc[0])
        f1 = int(sub["frame_number"].iloc[-1])
        head = sub.head(3)
        tail = sub.tail(3)
        frags.append({
            "wid": int(wid),
            "f_start": f0, "f_end": f1,
            "x_start": float(head["coord_x"].mean()),
            "y_start": float(head["coord_y"].mean()),
            "x_end": float(tail["coord_x"].mean()),
            "y_end": float(tail["coord_y"].mean()),
        })
    if not frags:
        return {}, 0

    F = pd.DataFrame(frags).sort_values("f_end").reset_index(drop=True)
    F_by_start = F.sort_values("f_start").reset_index(drop=True)
    start_frames = F_by_start["f_start"].values

    parent = {int(w): int(w) for w in F["wid"]}

    def find(w: int) -> int:
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    claimed: set[int] = set()
    ambig_skips = 0
    win_span = int(T_MAX_S * fps)
    ambig_win = AMBIG_TIME_WINDOW_S * fps

    for _, A in F.iterrows():
        f_end = A["f_end"]
        lo = np.searchsorted(start_frames, f_end + 1, side="left")
        hi = np.searchsorted(start_frames, f_end + win_span, side="right")
        candidates: list[tuple[float, int, int]] = []
        a_wid = int(A["wid"])
        a_root = find(a_wid)
        for i in range(lo, hi):
            B = F_by_start.iloc[i]
            b_wid = int(B["wid"])
            if b_wid == a_wid or b_wid in claimed:
                continue
            if find(b_wid) == a_root:
                continue
            dx = B["x_start"] - A["x_end"]
            dy = B["y_start"] - A["y_end"]
            d = float((dx * dx + dy * dy) ** 0.5)
            if d <= D_MAX:
                candidates.append((d, b_wid, int(B["f_start"])))
        if not candidates:
            continue
        candidates.sort()
        best_d, best_wid, best_fstart = candidates[0]
        competing = [c for c in candidates[1:]
                     if abs(c[2] - best_fstart) <= ambig_win]
        second_d = competing[0][0] if competing else float("inf")
        is_noise = best_d < AMBIG_FLOOR_PX and second_d < AMBIG_FLOOR_PX
        if (not is_noise) and second_d < AMBIG_RATIO * best_d:
            ambig_skips += 1
            continue
        parent[find(a_wid)] = find(best_wid)
        claimed.add(best_wid)

    groups: dict[int, list[int]] = {}
    for w in F["wid"]:
        w = int(w)
        groups.setdefault(find(w), []).append(w)
    return groups, ambig_skips
