"""
Fragment linker for the crawling pipeline — occupancy-based reconnection.

The rule this implements
------------------------
1. A SHORT skeleton/segmentation dropout is just a dropout: reconnect, no
   questions asked.
2. A LONGER gap is reconnected only if NO OTHER WORM was near the endpoint
   while the track was missing. An isolated worm that vanishes and reappears
   in the same place is the same worm — that is the motility "curl" case.
3. If another worm WAS near, identity cannot be recovered, so we do not try.
   The fragments stay separate and each becomes its own track. n is then a
   count of TRACKS, not of animals, which is the intended trade: a track that
   is certainly one animal for its whole length beats a longer track that
   might be two animals spliced together.

Why the previous linker under-linked
------------------------------------
It refused a link whenever a competing fragment lay within AMBIG_RATIO (2.0x)
of the best candidate's distance, with only a 3 px absolute floor. Measured on
the day-1 N2 10J reference video, candidate links within 150 px / 5 s have a
median end-to-start distance of 2 px at gaps under 0.5 s, 6 px at 0.5-1 s,
13 px at 1-2 s and 18 px at 2-5 s. At a best distance of 6 px the old rule
rejected any competitor closer than 12 px — which at that scale is noise, not
a rival animal. It made 87 merges out of 145 fragments and refused 12 links
outright.

At the same time 81% of fragment ends had no other worm within 60 px, so the
overwhelming majority of breaks were never ambiguous in the first place. The
question "is another animal close enough to be confused with this one" is
about OCCUPANCY of the plate, not about the relative distances of two
candidate successors, so that is what is tested here.

Distances scale with the gap because a worm keeps crawling while it is
missing: MAX_DIST_PX_AT_1S grows with elapsed time, capped at MAX_DIST_PX.

Merge detection
---------------
A collision does not always break a Tierpsy fragment. Often the blob simply
absorbs the second animal and the fragment continues, with a much larger area
and no skeleton, until they separate. On the reference video 13 of 42
fragments longer than 10 s showed such an episode. split_merges() cuts those
episodes out: the fragment is split at the merge, the merged interval is
dropped, and the pieces either side become separate tracks under rule 3.

Both functions work on a *_skeletons.hdf5 trajectories_data frame and never
touch feature data, so re-tuning any threshold here costs no Tierpsy re-run.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --- reconnection ---------------------------------------------------------
SHORT_GAP_S: float = 0.5        # at or below this, reconnect unconditionally
T_MAX_S: float = 5.0            # longest gap that may be bridged at all
MAX_DIST_PX_AT_1S: float = 45.0 # allowed end->start distance after a 1 s gap
MAX_DIST_PX: float = 150.0      # absolute ceiling regardless of gap
SHORT_GAP_DIST_PX: float = 30.0 # ceiling for the unconditional short-gap case

# --- occupancy ------------------------------------------------------------
# "Another worm was near the endpoint while this track was missing." 60 px is
# ~0.4 body lengths at the reference magnification (median worm ~150 px long),
# close enough that two animals could be confused for one another.
OCCUPANCY_PX: float = 60.0

# --- merge episodes -------------------------------------------------------
MERGE_AREA_RATIO: float = 1.6   # x the fragment's own 25th-percentile area
MERGE_MIN_FRAMES: int = 5       # shorter excursions are noise, not a collision
MERGE_PAD_FRAMES: int = 3       # also drop this many frames either side


# ---------------------------------------------------------------------------
# merge splitting
# ---------------------------------------------------------------------------

def split_merges(traj: pd.DataFrame) -> "tuple[pd.DataFrame, dict]":
    """
    Cut fragments at merge episodes and drop the merged frames.

    A merge episode is a run of >= MERGE_MIN_FRAMES frames in which the blob's
    area exceeds MERGE_AREA_RATIO x that fragment's own 25th-percentile area.
    Using the fragment's own baseline rather than a plate-wide one keeps this
    honest for a large worm among small ones.

    Returns (traj_with_split_ids, log). The returned frame carries a new
    'frag_id' column: the original worm_index_joined where nothing was cut, and
    <original>_<piece> where it was. Rows inside a merge episode are removed —
    they belong to no single animal and their skeletons are absent anyway.
    """
    required = {"worm_index_joined", "frame_number", "area"}
    if traj is None or len(traj) == 0 or not required.issubset(traj.columns):
        out = traj.copy() if traj is not None else traj
        if out is not None and len(out):
            out["frag_id"] = out["worm_index_joined"].astype(str)
        return out, {"merge_episodes": 0, "merge_frames_dropped": 0,
                     "fragments_split": 0}

    pieces: list[pd.DataFrame] = []
    n_ep = n_dropped = n_split = 0

    for wid, sub in traj.groupby("worm_index_joined"):
        sub = sub.sort_values("frame_number").reset_index(drop=True)
        area = sub["area"].values.astype(float)
        base = float(np.nanpercentile(area[np.isfinite(area)], 25)) if np.isfinite(area).any() else np.nan
        if not np.isfinite(base) or base <= 0:
            sub = sub.copy(); sub["frag_id"] = str(wid); pieces.append(sub); continue

        hot = np.isfinite(area) & (area > MERGE_AREA_RATIO * base)
        # keep only runs long enough to be a real collision, then pad them
        mask = np.zeros(len(sub), dtype=bool)
        i = 0
        while i < len(hot):
            if hot[i]:
                j = i
                while j < len(hot) and hot[j]:
                    j += 1
                if j - i >= MERGE_MIN_FRAMES:
                    mask[max(0, i - MERGE_PAD_FRAMES):min(len(sub), j + MERGE_PAD_FRAMES)] = True
                    n_ep += 1
                i = j
            else:
                i += 1

        if not mask.any():
            sub = sub.copy(); sub["frag_id"] = str(wid); pieces.append(sub); continue

        n_dropped += int(mask.sum())
        # contiguous clean spans become separate pieces
        spans, in_run, start = [], False, 0
        for k, bad in enumerate(mask):
            if not bad and not in_run:
                start, in_run = k, True
            elif bad and in_run:
                spans.append((start, k)); in_run = False
        if in_run:
            spans.append((start, len(sub)))
        spans = [(a, b) for a, b in spans if b > a]
        if len(spans) > 1:
            n_split += 1
        for pi, (a, b) in enumerate(spans):
            piece = sub.iloc[a:b].copy()
            piece["frag_id"] = f"{wid}_{pi}" if len(spans) > 1 else str(wid)
            pieces.append(piece)

    out = (pd.concat(pieces, ignore_index=True) if pieces
           else traj.iloc[0:0].assign(frag_id=[]))
    return out, {"merge_episodes": n_ep, "merge_frames_dropped": n_dropped,
                 "fragments_split": n_split}


# ---------------------------------------------------------------------------
# linking
# ---------------------------------------------------------------------------

def _max_dist_for_gap(gap_s: float) -> float:
    if gap_s <= SHORT_GAP_S:
        return SHORT_GAP_DIST_PX
    return min(MAX_DIST_PX, MAX_DIST_PX_AT_1S * max(gap_s, 1.0))


def link_fragments(
    trajectories_data: pd.DataFrame,
    fps: float,
    split_on_merge: bool = True,
) -> "tuple[dict, int, dict, pd.DataFrame]":
    """
    Group fragments into tracks.

    Returns (groups, refused, log, traj_split):
        groups     — {group_id: [frag_id, ...]}; frag_id is the (possibly
                     split) fragment identifier, always a str.
        refused    — links refused because the plate was occupied.
        log        — counters for the analysis_log sidecar.
        traj_split — the input rows minus any dropped merge frames, carrying
                     the 'frag_id' column. Callers must index off THIS rather
                     than the original table, because a split fragment's pieces
                     share one worm_index_joined but are different tracks.
    """
    required = {"worm_index_joined", "frame_number", "coord_x", "coord_y"}
    empty_log = {"input_track_count": 0, "merge_episodes": 0,
                 "merge_frames_dropped": 0, "fragments_split": 0,
                 "links_made_short_gap": 0, "links_made_isolated": 0,
                 "links_refused_occupied": 0, "links_refused_no_candidate": 0}
    _empty = (trajectories_data.iloc[0:0].assign(frag_id=[])
              if trajectories_data is not None else pd.DataFrame())
    if trajectories_data is None or len(trajectories_data) == 0:
        return {}, 0, empty_log, _empty
    if not required.issubset(trajectories_data.columns):
        return {}, 0, empty_log, _empty

    n_input = int(trajectories_data["worm_index_joined"].nunique())

    if split_on_merge:
        traj, mlog = split_merges(trajectories_data)
    else:
        traj = trajectories_data.copy()
        traj["frag_id"] = traj["worm_index_joined"].astype(str)
        mlog = {"merge_episodes": 0, "merge_frames_dropped": 0, "fragments_split": 0}
    if traj is None or not len(traj):
        return {}, 0, {**empty_log, **mlog, "input_track_count": n_input}, _empty

    # --- one record per fragment -----------------------------------------
    frags = []
    for fid, sub in traj.groupby("frag_id"):
        sub = sub.sort_values("frame_number")
        head, tail = sub.head(3), sub.tail(3)
        frags.append({
            "fid": fid,
            "f_start": int(sub["frame_number"].iloc[0]),
            "f_end": int(sub["frame_number"].iloc[-1]),
            "x_start": float(head["coord_x"].mean()),
            "y_start": float(head["coord_y"].mean()),
            "x_end": float(tail["coord_x"].mean()),
            "y_end": float(tail["coord_y"].mean()),
        })
    if not frags:
        return {}, 0, {**empty_log, **mlog, "input_track_count": n_input}, traj

    F = pd.DataFrame(frags).sort_values("f_end").reset_index(drop=True)
    F_by_start = F.sort_values("f_start").reset_index(drop=True)
    start_frames = F_by_start["f_start"].values

    # --- per-frame positions, for the occupancy test ---------------------
    # Every tracked centroid in a frame, including fragments not involved in
    # the link. That is the point: the question is whether ANOTHER animal was
    # near enough to be confused with this one, not whether another fragment
    # happened to start here.
    pos_by_frame: dict[int, tuple] = {}
    for f, sub in traj.groupby("frame_number"):
        pos_by_frame[int(f)] = (sub["coord_x"].values.astype(float),
                                sub["coord_y"].values.astype(float),
                                sub["frag_id"].values)

    def occupied(x: float, y: float, f0: int, f1: int, own: set) -> bool:
        """Was any other worm within OCCUPANCY_PX of (x, y) during [f0, f1]?"""
        step = max(1, (f1 - f0) // 12)   # sampling; the worm is slow
        for f in range(f0, f1 + 1, step):
            e = pos_by_frame.get(f)
            if e is None:
                continue
            xs, ys, ids = e
            m = ~np.isin(ids, list(own))
            if not m.any():
                continue
            if np.min(np.hypot(xs[m] - x, ys[m] - y)) < OCCUPANCY_PX:
                return True
        return False

    parent = {r: r for r in F["fid"]}
    members = {r: {r} for r in F["fid"]}

    def find(w):
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    claimed: set = set()
    n_short = n_iso = n_occ = n_none = 0
    win = int(T_MAX_S * fps)

    for _, A in F.iterrows():
        f_end = int(A["f_end"])
        a_fid = A["fid"]
        a_root = find(a_fid)
        lo = np.searchsorted(start_frames, f_end + 1, side="left")
        hi = np.searchsorted(start_frames, f_end + win, side="right")

        best = None
        for i in range(lo, hi):
            B = F_by_start.iloc[i]
            b_fid = B["fid"]
            if b_fid == a_fid or b_fid in claimed or find(b_fid) == a_root:
                continue
            gap_s = (int(B["f_start"]) - f_end) / fps
            d = float(np.hypot(B["x_start"] - A["x_end"], B["y_start"] - A["y_end"]))
            if d > _max_dist_for_gap(gap_s):
                continue
            if best is None or d < best[0]:
                best = (d, b_fid, int(B["f_start"]), gap_s)
        if best is None:
            n_none += 1
            continue

        d, b_fid, b_start, gap_s = best
        if gap_s <= SHORT_GAP_S:
            n_short += 1                       # rule 1: just a dropout
        else:
            own = members[a_root] | members[find(b_fid)]
            if occupied(float(A["x_end"]), float(A["y_end"]),
                        f_end + 1, b_start - 1, own):
                n_occ += 1                     # rule 3: cannot know — leave broken
                continue
            n_iso += 1                         # rule 2: isolated, same animal

        ra, rb = find(a_fid), find(b_fid)
        parent[ra] = rb
        members[rb] = members[rb] | members[ra]
        claimed.add(b_fid)

    groups: dict = {}
    for w in F["fid"]:
        groups.setdefault(find(w), []).append(w)

    log = {
        "input_track_count": n_input,
        **mlog,
        "fragments_after_split": int(len(F)),
        "groups_formed": len(groups),
        "links_made_short_gap": n_short,
        "links_made_isolated": n_iso,
        "links_refused_occupied": n_occ,
        "links_refused_no_candidate": n_none,
    }
    return groups, n_occ, log, traj
