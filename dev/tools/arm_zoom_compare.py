#!/usr/bin/env python3
"""
arm_zoom_compare.py — decide the segmentation-width question by eye.

The open question after phase 1: arm6 (worm_bw_thresh_factor 1.05) reports a
higher raw skeleton yield than arm7 (0.939 vs 0.909), but its worms come out
12% shorter, 26% thinner and 40% smaller in area, and Tierpsy's own SKE_FILT
rejects 13.5% of its skeletons against arm7's 4.6%. Either 1.05 is eroding the
animal, or 0.92 was bloating it and 1.05 is the honest width. Yield cannot
answer that; only looking at the boundary can.

So this crops the SAME worm from the SAME raw pixels for every arm and draws,
per arm, that arm's own segmented outline and skeleton:

    green   contour_side1 + contour_side2 — the actual boundary Tierpsy
            skeletonised, straight out of the arm's *_skeletons.hdf5
    orange  the 49-point skeleton
    dots    the skeleton's two endpoints (head/tail)

What to look for
----------------
  * Does the green outline sit ON the visible edge of the worm, or INSIDE it?
    Inside = erosion. Outside, into the halo = bloat.
  * Do the endpoint dots reach the actual nose and tail tip, or stop short?
    A too-thin segmentation loses the tapered ends first, which is exactly how
    a skeleton gets 12% shorter without looking obviously wrong.
  * Compare at the RIM as well as the centre — that is where the arms differ.

Rows are worms picked across the radius; columns are the arms.

Usage:

    python dev/tools/arm_zoom_compare.py ^
        --root "C:\\Users\\Isabe\\Documents\\WormScan\\test\\skeleton_arms" ^
        --arms arm1_sub,arm6_sub_bw105,arm7_sub_block61

Writes zoom_compare_f<frame>.png into --root (one montage per sampled frame).
Add --frames 900,2400,4200 to choose your own, --n-worms to change the rows.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

CROP = 260          # px of original frame per cell, centred on the worm
ZOOM = 3            # upscale factor for the cell
C_CONTOUR = (80, 220, 80)
C_SKEL = (40, 150, 255)
C_END = (255, 255, 255)


def _load_arm(root: Path, arm: str):
    """(trajectories_data, skeleton, contour_side1, contour_side2) for one arm."""
    import h5py
    import pandas as pd

    res = root / arm / "Results"
    cands = sorted(res.glob("*_skeletons.hdf5"))
    if not cands:
        raise FileNotFoundError(f"no *_skeletons.hdf5 under {res}")
    p = cands[0]
    t = pd.read_hdf(str(p), key="trajectories_data")
    with h5py.File(str(p), "r") as f:
        skel = f["skeleton"][:]
        c1 = f["contour_side1"][:] if "contour_side1" in f else None
        c2 = f["contour_side2"][:] if "contour_side2" in f else None
    return t[t.has_skeleton > 0.5], skel, c1, c2


def _pick_worms(arms_data: dict, frame: int, n: int, wh):
    """
    Worms skeletonised in EVERY arm at this frame, spread across the radius so
    the montage covers centre and rim rather than whatever happens to be first.
    Matching is positional — worm_index_joined is not comparable between arms.
    """
    W, H = wh
    ref = next(iter(arms_data.values()))[0]
    here = ref[ref.frame_number == frame]
    picks = []
    for row in here.itertuples():
        x, y = float(row.coord_x), float(row.coord_y)
        if not (CROP // 2 < x < W - CROP // 2 and CROP // 2 < y < H - CROP // 2):
            continue
        hit = {}
        for arm, (t, *_rest) in arms_data.items():
            sub = t[t.frame_number == frame]
            if not len(sub):
                break
            d = np.hypot(sub.coord_x.values - x, sub.coord_y.values - y)
            j = int(np.argmin(d))
            if d[j] > 25:            # same animal in this arm, or skip it
                break
            hit[arm] = sub.iloc[j]
        if len(hit) == len(arms_data):
            picks.append((np.hypot(x - W / 2, y - H / 2), x, y, hit))
    if not picks:
        return []
    picks.sort(key=lambda p: p[0])
    idx = np.linspace(0, len(picks) - 1, min(n, len(picks))).astype(int)
    return [picks[i] for i in idx]


def _draw(cell, row, skel, c1, c2, x0, y0):
    import cv2

    sid = int(row.skeleton_id)
    def put(arr, colour, thick):
        if arr is None or not (0 <= sid < len(arr)):
            return None
        pts = arr[sid]
        if not np.isfinite(pts).all():
            return None
        p = ((pts - np.array([x0, y0])) * ZOOM).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(cell, [p], False, colour, thick, cv2.LINE_AA)
        return pts

    put(c1, C_CONTOUR, 2)
    put(c2, C_CONTOUR, 2)
    s = put(skel, C_SKEL, 2)
    if s is not None:
        for e in (s[0], s[-1]):
            cv2.circle(cell, tuple(((e - np.array([x0, y0])) * ZOOM).astype(int)),
                       5, C_END, -1, cv2.LINE_AA)
        seg = np.diff(s, axis=0)
        return float(np.sum(np.hypot(seg[:, 0], seg[:, 1])))
    return float("nan")


def main() -> None:
    import cv2

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--arms", default="arm1_sub,arm6_sub_bw105,arm7_sub_block61")
    ap.add_argument("--frames", default="900,2400,4200")
    ap.add_argument("--n-worms", type=int, default=5)
    ap.add_argument("--um-per-px", type=float, default=10.19,
                    help="only used for the printed annotation; 0 disables it")
    args = ap.parse_args()

    root = Path(args.root)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    raw = sorted(p for p in root.glob("*.avi")
                 if not any(p.stem.endswith(s) for s in ("_sub", "_div", "_subx")))
    if not raw:
        raise SystemExit(f"no raw AVI in {root}")
    raw = raw[0]
    print(f"underlay: {raw.name}  (identical pixels for every arm)")

    data = {a: _load_arm(root, a) for a in arms}
    cap = cv2.VideoCapture(str(raw))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    for frame in [int(f) for f in args.frames.split(",") if f.strip()]:
        picks = _pick_worms(data, frame, args.n_worms, (W, H))
        if not picks:
            print(f"frame {frame}: no worm skeletonised in all arms, skipping")
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, img = cap.read()
        if not ok:
            print(f"frame {frame}: could not read")
            continue
        gray = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)

        cell = CROP * ZOOM
        head = 46
        mont = np.zeros((head + cell * len(picks), cell * len(arms), 3), np.uint8)
        for ci, arm in enumerate(arms):
            cv2.putText(mont, arm, (ci * cell + 14, 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)

        for ri, (rad, x, y, hit) in enumerate(picks):
            x0, y0 = int(x - CROP // 2), int(y - CROP // 2)
            patch = gray[y0:y0 + CROP, x0:x0 + CROP]
            for ci, arm in enumerate(arms):
                t, skel, c1, c2 = data[arm]
                c = cv2.resize(patch, (cell, cell), interpolation=cv2.INTER_NEAREST)
                L = _draw(c, hit[arm], skel, c1, c2, x0, y0)
                lab = f"len {L:.0f}px"
                if args.um_per_px:
                    lab += f" ~{L * args.um_per_px:.0f}um"
                cv2.rectangle(c, (0, cell - 34), (cell, cell), (0, 0, 0), -1)
                cv2.putText(c, lab, (10, cell - 10), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (255, 255, 255), 2, cv2.LINE_AA)
                if ci == 0:
                    cv2.putText(c, f"r={rad:.0f}px", (10, 28),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
                mont[head + ri * cell:head + (ri + 1) * cell,
                     ci * cell:(ci + 1) * cell] = c

        out = root / f"zoom_compare_f{frame}.png"
        cv2.imwrite(str(out), mont)
        print(f"wrote {out.name}  ({len(picks)} worms, r "
              f"{picks[0][0]:.0f}-{picks[-1][0]:.0f}px)")
    cap.release()
    print("\ngreen = that arm's segmented contour, orange = skeleton, "
          "white dots = skeleton ends.\nContour inside the visible worm edge "
          "means erosion; endpoints short of the nose/tail is the same thing.")


if __name__ == "__main__":
    main()
