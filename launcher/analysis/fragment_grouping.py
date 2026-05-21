"""
Steps 1 and 2 of the motility pipeline: group spatiotemporally adjacent Tierpsy
fragments and classify each group as curl (linear chain) or collision (branching).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np
import pandas as pd


@dataclass
class FragmentInfo:
    track_id: int
    start_frame: int
    end_frame: int
    start_pos: Tuple[float, float]   # mean centroid of first 5 frames (x, y)
    end_pos: Tuple[float, float]     # mean centroid of last 5 frames (x, y)
    wall_clock_start_s: float
    wall_clock_end_s: float


@dataclass
class WormGroup:
    virtual_worm_id: int
    track_ids: List[int]        # all worm_index_joined in this group
    classification: str         # 'curl' or 'collision'
    repr_track_id: int          # trajectory to use for head-angle plots / render
    curl_count: int             # n_fragments - 1 for curl, 0 for collision
    fragment_count: int


def build_fragment_infos(traj_df: pd.DataFrame, fps: float) -> List[FragmentInfo]:
    """
    One FragmentInfo per unique worm_index_joined. Requires coord_x / coord_y columns
    (standard Tierpsy trajectories_data); falls back to NaN positions if absent.
    """
    frame_col = "timestamp_raw" if "timestamp_raw" in traj_df.columns else "frame_number"
    has_pos = "coord_x" in traj_df.columns and "coord_y" in traj_df.columns

    infos: List[FragmentInfo] = []
    for track_id, grp in traj_df.groupby("worm_index_joined"):
        grp = grp.sort_values(frame_col).reset_index(drop=True)
        frames = grp[frame_col].values.astype(float)

        if has_pos:
            xs = grp["coord_x"].values.astype(float)
            ys = grp["coord_y"].values.astype(float)
        else:
            xs = np.full(len(grp), np.nan)
            ys = np.full(len(grp), np.nan)

        n5 = min(5, len(grp))
        start_pos = (float(np.nanmean(xs[:n5])), float(np.nanmean(ys[:n5])))
        end_pos   = (float(np.nanmean(xs[-n5:])), float(np.nanmean(ys[-n5:])))

        start_frame = int(frames[0])
        end_frame   = int(frames[-1])

        infos.append(FragmentInfo(
            track_id=int(track_id),
            start_frame=start_frame,
            end_frame=end_frame,
            start_pos=start_pos,
            end_pos=end_pos,
            wall_clock_start_s=start_frame / fps,
            wall_clock_end_s=end_frame / fps,
        ))
    return infos


def _adjacent_pairs(
    infos: List[FragmentInfo],
    distance_threshold_px: float,
    time_gap_threshold_s: float,
) -> List[Tuple[int, int]]:
    """
    Return (i, j) index pairs where track i ends before track j starts and
    both time gap and spatial distance are within thresholds.
    """
    pairs: List[Tuple[int, int]] = []
    for i, a in enumerate(infos):
        for j, b in enumerate(infos):
            if i == j:
                continue
            gap_s = b.wall_clock_start_s - a.wall_clock_end_s
            if gap_s < 0 or gap_s > time_gap_threshold_s:
                continue
            dx = b.start_pos[0] - a.end_pos[0]
            dy = b.start_pos[1] - a.end_pos[1]
            if (dx * dx + dy * dy) ** 0.5 <= distance_threshold_px:
                pairs.append((i, j))
    return pairs


def _connected_components(n: int, edges: List[Tuple[int, int]]) -> List[List[int]]:
    """Union-find over n nodes."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    for a, b in edges:
        union(a, b)

    groups: dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def group_fragments(
    traj_df: pd.DataFrame,
    fps: float,
    distance_threshold_px: float,
    time_gap_threshold_s: float,
) -> List[WormGroup]:
    """
    Main entry point. Returns one WormGroup per spatiotemporal cluster.
    Solo tracks form groups of size 1, classified as curl.
    """
    infos = build_fragment_infos(traj_df, fps)
    if not infos:
        return []

    adj = _adjacent_pairs(infos, distance_threshold_px, time_gap_threshold_s)
    components = _connected_components(len(infos), adj)

    # Build per-node degree maps restricted to the same component
    out_of: dict[int, List[int]] = defaultdict(list)
    in_of: dict[int, List[int]] = defaultdict(list)
    for a, b in adj:
        out_of[a].append(b)
        in_of[b].append(a)

    groups: List[WormGroup] = []
    for virtual_id, component in enumerate(components):
        comp_set = set(component)
        track_ids = [infos[i].track_id for i in component]

        # Curl = every node has at most one incoming and one outgoing edge within component
        is_curl = all(
            len([e for e in out_of[i] if e in comp_set]) <= 1
            and len([e for e in in_of[i]  if e in comp_set]) <= 1
            for i in component
        )
        classification = "curl" if is_curl else "collision"

        sorted_by_start = sorted(component, key=lambda i: infos[i].wall_clock_start_s)
        repr_idx = sorted_by_start[0]   # updated to longest sub-track in metrics layer for collisions

        groups.append(WormGroup(
            virtual_worm_id=virtual_id,
            track_ids=track_ids,
            classification=classification,
            repr_track_id=infos[repr_idx].track_id,
            curl_count=(len(component) - 1) if is_curl else 0,
            fragment_count=len(component),
        ))

    return groups
