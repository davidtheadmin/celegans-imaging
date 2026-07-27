#!/usr/bin/env python3
"""
stage_conf_report.py — read the staging model's confidence distribution off a
real plate set, so vision/stage_conf.json can be set from data instead of taste.

The numbers currently in stage_conf.json were CHOSEN, not calibrated (see the
_README block in that file, and "Staging model — per-class confidence
thresholds" in BACKLOG.md). This script is the thing that replaces them.

Run it in the VISION venv, not the launcher venv — it imports tiled_infer and
loads the model:

    launcher\\vision\\.venv-vision\\Scripts\\python.exe ^
        dev\\tools\\stage_conf_report.py "C:\\path\\to\\plate\\images"

What it reports
---------------
1. Per-class confidence histogram, at a deliberately low floor (0.05), plus the
   count that would survive each candidate threshold from 0.05 to 0.60. Read a
   threshold off the point where the count stops falling steeply: the steep part
   is noise dying, the flat part is real worms. If a class has no flat part, the
   model cannot separate it and no threshold will fix that — retrain instead.

2. Box-size percentiles per class, in full-frame pixels. This sets the tile
   overlap: a worm is only guaranteed to sit fully inside some tile when its box
   is no larger than (tile - step). At overlap 0.20 that is 135x122 px; at 0.35
   it is 237x213 px. If the 95th-percentile box is bigger than the guarantee,
   worms are being sliced by seams and the overlap should go up.

3. Seam-fragment stats: how many detections touch an interior tile seam, and how
   many of those are covered by a better box (i.e. how many duplicates the
   suppression pass in tiled_infer is actually removing on YOUR images). Also
   reports cross-class duplicate pairs — the same worm called two different
   stages in two different tiles, which per-class NMS alone cannot merge and
   which biases survival % directly when the pair straddles the L2/L3 cutoff.

4. Size plausibility per class, as sqrt(w*h) percentiles, plus a check that
   median size actually rises along egg -> L1 -> ... -> adult. This is what sets
   `class_size_px` in stage_conf.json, which is the only handle on debris that
   scores HIGH on a stage it cannot possibly be — a small speck called "adult"
   is not an uncertain adult, and no confidence threshold touches it. With
   --suggest, writes a paste-ready block plus what each bound would remove.

   Read the removal counts before pasting. These percentiles come from the
   model's own detections, so a class already polluted with debris has its lower
   bound set BY that debris and the gate will not remove it. For a class you know
   is contaminated, set its lower bound by hand from the next stage down instead.

Nothing is written back automatically. Read the report, then edit
vision/stage_conf.json yourself.

Outputs <out>/stage_conf_report.txt, stage_conf_raw.csv (one row per raw
detection, so you can re-cut the analysis in Excel without re-running the model),
and with --suggest, stage_conf_suggested.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Import the shared library the real pipeline uses; never a private copy.
_VISION = Path(__file__).resolve().parents[2] / "launcher" / "vision"
sys.path.insert(0, str(_VISION))

import numpy as np                                      # noqa: E402
from PIL import Image                                   # noqa: E402

from tiled_infer import (box_size_px, covered_fraction,  # noqa: E402
                         tile_origins)

# Developmental order. Used ONLY to sanity-check a suggested size block: median
# box size must rise along this sequence, and where it does not, the model is
# not separating those stages by size and a size gate cannot be trusted there.
_STAGE_ORDER = ["egg", "L1", "L2", "L3", "L4", "young adult", "adult"]

_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3

_SWEEP = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60]
_FLOOR = 0.05          # collect everything; thresholds are applied afterwards
_TILE_W, _TILE_H = 676, 608
_IMGSZ = 640


def find_images(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    out: list[Path] = []

    def _rec(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for child in sorted(path.iterdir()):
            if child.is_file() and child.suffix.lower() in _IMAGE_EXTS:
                out.append(child)
            elif child.is_dir() and not child.name.startswith(("_", ".")):
                _rec(child, depth + 1)

    _rec(folder, 1)
    return out


def raw_detections(img_path: Path, model, names, overlap: float, seam_margin: int):
    """Every raw tile detection at _FLOOR, with its seam flag — i.e. exactly
    what tiled_infer sees before it merges anything. Deliberately duplicated
    here rather than reusing tiled_infer(), because the whole point is to
    inspect the pre-merge population that tiled_infer throws away."""
    arr = np.array(Image.open(img_path).convert("RGB"))
    H, W = arr.shape[:2]
    dets = []
    for oy in tile_origins(H, _TILE_H, overlap):
        for ox in tile_origins(W, _TILE_W, overlap):
            tile = arr[oy:oy + _TILE_H, ox:ox + _TILE_W]
            th, tw = tile.shape[:2]
            res = model.predict(np.ascontiguousarray(tile[:, :, ::-1]),
                                imgsz=_IMGSZ, conf=_FLOOR, verbose=False)[0]
            if res.boxes is None or len(res.boxes) == 0:
                continue
            for (bx1, by1, bx2, by2), sc, ci in zip(
                res.boxes.xyxy.cpu().numpy(),
                res.boxes.conf.cpu().numpy(),
                res.boxes.cls.cpu().numpy().astype(int),
            ):
                truncated = bool(
                    (ox > 0 and bx1 <= seam_margin) or
                    (ox + tw < W and bx2 >= tw - seam_margin) or
                    (oy > 0 and by1 <= seam_margin) or
                    (oy + th < H and by2 >= th - seam_margin)
                )
                dets.append({
                    "image": img_path.name,
                    "stage": names.get(int(ci), str(ci)),
                    "score": float(sc),
                    "x1": ox + float(bx1), "y1": oy + float(by1),
                    "x2": ox + float(bx2), "y2": oy + float(by2),
                    "size_px": box_size_px(bx1, by1, bx2, by2),
                    "truncated": truncated,
                })
    return dets, (W, H)


def _pct(values, q):
    return float(np.percentile(values, q)) if len(values) else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of plate images (walked 3 deep)")
    ap.add_argument("--model", default=str(_VISION / "models" / "staging.pt"))
    ap.add_argument("--out", help="output dir (default: <folder>/_stage_conf_report)")
    ap.add_argument("--overlap", type=float, default=0.35,
                    help="tile overlap to profile (default 0.35)")
    ap.add_argument("--seam-margin", type=int, default=12)
    ap.add_argument("--cover-frac", type=float, default=0.6)
    ap.add_argument("--limit", type=int, default=0,
                    help="profile only the first N images (0 = all)")
    ap.add_argument("--suggest", action="store_true",
                    help="also write stage_conf_suggested.json with a paste-ready "
                         "class_size_px block")
    ap.add_argument("--size-lo-pct", type=float, default=2.0,
                    help="percentile for the suggested lower size bound (2.0)")
    ap.add_argument("--size-hi-pct", type=float, default=98.0,
                    help="percentile for the suggested upper size bound (98.0)")
    args = ap.parse_args()

    from ultralytics import YOLO

    folder = Path(args.folder).resolve()
    images = find_images(folder)
    if args.limit:
        images = images[:args.limit]
    if not images:
        print(f"No images under {folder}", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else folder / "_stage_conf_report"
    out_dir.mkdir(parents=True, exist_ok=True)

    model = YOLO(args.model)
    names = model.names
    print(f"model classes: {list(names.values())}", file=sys.stderr)

    all_dets: list[dict] = []
    frame_size = None
    for i, img in enumerate(images, 1):
        print(f"[{i}/{len(images)}] {img.name}", file=sys.stderr)
        dets, frame_size = raw_detections(img, model, names, args.overlap,
                                          args.seam_margin)
        all_dets.extend(dets)

    # ---- raw CSV ----------------------------------------------------------
    csv_path = out_dir / "stage_conf_raw.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_dets[0].keys()))
        w.writeheader()
        w.writerows(all_dets)

    # ---- report -----------------------------------------------------------
    lines: list[str] = []
    lines.append(f"stage_conf_report — {len(images)} image(s) under {folder}")
    lines.append(f"frame {frame_size[0]}x{frame_size[1]}, tile {_TILE_W}x{_TILE_H} "
                 f"@ overlap {args.overlap}, floor {_FLOOR}")
    step_x = max(1, int(round(_TILE_W * (1 - args.overlap))))
    step_y = max(1, int(round(_TILE_H * (1 - args.overlap))))
    guar_w, guar_h = _TILE_W - step_x, _TILE_H - step_y
    n_tiles = (len(tile_origins(frame_size[0], _TILE_W, args.overlap))
               * len(tile_origins(frame_size[1], _TILE_H, args.overlap)))
    lines.append(f"{n_tiles} tiles/frame; whole-object guarantee "
                 f"{guar_w}x{guar_h} px")
    lines.append("")

    by_stage: dict[str, list[dict]] = defaultdict(list)
    for d in all_dets:
        by_stage[d["stage"]].append(d)

    lines.append("=== 1. surviving detections vs threshold (per class) ===")
    lines.append("stage".ljust(14) + "".join(f"{t:>7.2f}" for t in _SWEEP))
    for stage in (names[i] for i in sorted(names)):
        row = by_stage.get(stage, [])
        counts = [sum(1 for d in row if d["score"] >= t) for t in _SWEEP]
        lines.append(stage.ljust(14) + "".join(f"{c:>7d}" for c in counts))
    lines.append("")
    lines.append("Read the threshold off where the count flattens: the steep")
    lines.append("part is noise dying, the flat part is real worms. A class")
    lines.append("that never flattens is a retrain problem, not a threshold one.")
    lines.append("")

    lines.append("=== 2. box size percentiles, px (full-frame coords) ===")
    lines.append("stage".ljust(14) + "n".rjust(7)
                 + "".join(s.rjust(10) for s in
                           ("w_p50", "w_p95", "w_max", "h_p50", "h_p95", "h_max")))
    for stage in (names[i] for i in sorted(names)):
        row = [d for d in by_stage.get(stage, []) if d["score"] >= 0.25]
        ws = [d["x2"] - d["x1"] for d in row]
        hs = [d["y2"] - d["y1"] for d in row]
        if not ws:
            lines.append(stage.ljust(14) + f"{0:>7d}")
            continue
        lines.append(
            stage.ljust(14) + f"{len(ws):>7d}"
            + "".join(f"{v:>10.0f}" for v in (
                _pct(ws, 50), _pct(ws, 95), max(ws),
                _pct(hs, 50), _pct(hs, 95), max(hs)))
        )
    lines.append("")
    lines.append(f"Guarantee at this overlap is {guar_w}x{guar_h} px. Any class")
    lines.append("whose w_p95/h_p95 exceeds it is being sliced by tile seams on")
    lines.append("a meaningful fraction of worms — raise overlap in stage_conf.json.")
    lines.append("")

    lines.append("=== 3. seam fragments and cross-class duplicates ===")
    n_trunc = sum(1 for d in all_dets if d["truncated"])
    lines.append(f"raw detections           : {len(all_dets)}")
    lines.append(f"touching an interior seam: {n_trunc} "
                 f"({100.0 * n_trunc / max(1, len(all_dets)):.1f}%)")

    covered = 0
    cross_class = Counter()
    per_image: dict[str, list[dict]] = defaultdict(list)
    for d in all_dets:
        if d["score"] >= 0.25:
            per_image[d["image"]].append(d)
    for dets in per_image.values():
        ordered = sorted(dets, key=lambda d: -d["score"])
        for i, d in enumerate(ordered):
            box = (d["x1"], d["y1"], d["x2"], d["y2"])
            for better in ordered[:i]:
                obox = (better["x1"], better["y1"], better["x2"], better["y2"])
                if covered_fraction(box, obox) >= args.cover_frac:
                    if d["truncated"]:
                        covered += 1
                    if d["stage"] != better["stage"]:
                        cross_class[(better["stage"], d["stage"])] += 1
                    break
    lines.append(f"seam fragments a better box covers (would be suppressed): "
                 f"{covered}")
    lines.append("")
    lines.append("cross-class overlapping pairs (kept stage <- suppressed stage):")
    if not cross_class:
        lines.append("  (none)")
    for (keep, drop), n in cross_class.most_common(20):
        flag = "   <-- straddles the survivor cutoff" if (
            {keep, drop} & {"L2"} and {keep, drop} & {"L3"}) else ""
        lines.append(f"  {keep:>12} <- {drop:<12} {n:>5}{flag}")

    # ---- 4. size gate ------------------------------------------------------
    lines.append("")
    lines.append("=== 4. size plausibility, sqrt(w*h) px ===")
    lines.append("stage".ljust(14) + "n".rjust(7)
                 + "".join(s.rjust(9) for s in
                           ("p2", "p10", "p50", "p90", "p98")))
    size_by_stage: dict[str, list[float]] = {}
    for stage in (names[i] for i in sorted(names)):
        # Untruncated only: a seam-clipped worm is legitimately undersized, and
        # the gate exempts those, so they must not shape the suggested bounds.
        vals = sorted(d["size_px"] for d in by_stage.get(stage, [])
                      if d["score"] >= 0.25 and not d["truncated"])
        size_by_stage[stage] = vals
        if not vals:
            lines.append(stage.ljust(14) + f"{0:>7d}")
            continue
        lines.append(stage.ljust(14) + f"{len(vals):>7d}"
                     + "".join(f"{_pct(vals, q):>9.0f}"
                               for q in (2, 10, 50, 90, 98)))
    lines.append("")

    # Median size must rise along the developmental order. Where it does not,
    # the model is not separating those stages by size at all, and a size gate
    # there would be enforcing a distinction the detector cannot make.
    ordered = [s for s in _STAGE_ORDER if size_by_stage.get(s)]
    medians = {s: _pct(size_by_stage[s], 50) for s in ordered}
    breaks = [(a, b) for a, b in zip(ordered, ordered[1:])
              if medians[a] >= medians[b]]
    lines.append("median size along egg -> adult: "
                 + " < ".join(f"{s} {medians[s]:.0f}" for s in ordered))
    if breaks:
        lines.append("WARNING: size ordering is violated at "
                     + ", ".join(f"{a}/{b}" for a, b in breaks)
                     + " — the model is not separating those stages by size,")
        lines.append("so do NOT size-gate them. Fix by retraining, not by "
                     "tightening bounds.")
    else:
        lines.append("ordering holds — size is a usable discriminator here.")
    lines.append("")

    if args.suggest:
        block, removals = {}, []
        for stage in (names[i] for i in sorted(names)):
            vals = size_by_stage.get(stage) or []
            if len(vals) < 20:
                removals.append(f"  {stage:>12}: only {len(vals)} sample(s) — "
                                f"NOT suggested, too few to bound")
                continue
            lo = round(_pct(vals, args.size_lo_pct))
            hi = round(_pct(vals, args.size_hi_pct))
            block[stage] = [lo, hi]
            n_out = sum(1 for v in vals if v < lo or v > hi)
            removals.append(
                f"  {stage:>12}: [{lo}, {hi}] would drop {n_out}/{len(vals)} "
                f"({100.0 * n_out / len(vals):.1f}%) of this class")
        suggested = {"class_size_px": block}
        (out_dir / "stage_conf_suggested.json").write_text(
            json.dumps(suggested, indent=2) + "\n", encoding="utf-8")
        lines.append("=== suggested class_size_px "
                     f"(p{args.size_lo_pct:.0f}/p{args.size_hi_pct:.0f}) ===")
        lines.extend(removals)
        lines.append("")
        lines.append("Written to stage_conf_suggested.json. READ THE REMOVAL")
        lines.append("COUNTS BEFORE PASTING: these percentiles are cut from the")
        lines.append("model's own detections, so if a class is already polluted")
        lines.append("with debris, the debris sets the lower bound and the gate")
        lines.append("will not remove it. For a class you KNOW is contaminated")
        lines.append("(adult picking up specks), set its lower bound by hand from")
        lines.append("the next stage down instead — an adult cannot be smaller")
        lines.append("than a typical L4.")
        lines.append("")
        for a, b in breaks:
            lines.append(f"NOTE: {a}/{b} failed the ordering check above; "
                         f"consider dropping them from the pasted block.")

    report = "\n".join(lines) + "\n"
    (out_dir / "stage_conf_report.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"wrote {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
