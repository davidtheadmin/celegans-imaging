#!/usr/bin/env python3
"""Is the small end of the 'adult' class debris, or real worms?

Run this with the LAUNCHER venv (it needs numpy + cv2, not torch).

    launcher\\.venv\\Scripts\\python.exe dev\\tools\\check_adult_debris.py ^
        --csv  "C:\\path\\to\\_development_<stamp>\\soft_stage_scores.csv" ^
        --images "C:\\Users\\Isabe\\Documents\\WormScan\\experiments" ^
        --out   "C:\\Users\\Isabe\\Desktop\\adult_check"

WHY THIS EXISTS
---------------
`class_size_px["adult"]` ships a lower bound of 43 px. The median size of an L4
is 71 px and of an adult 79 px, so a "43 px adult" is smaller than a typical L2
- biologically impossible. The bound is that low because it was cut as a
percentile of the model's OWN adult detections, and those are exactly the
population the debris is polluting: the debris set its own bound. The gate
therefore removes 0.1% of detections and does nothing about the failure it was
built for.

The fix is NOT obvious, which is why this script measures instead of assuming:

  - If the small "adults" are debris, raising the floor is right and the current
    adult counts are inflated.
  - If they are real worms the model has mislabelled, raising the floor is
    WRONG. A size gate DELETES a detection, removing it from the numerator and
    the denominator alike. A mislabelled worm should be re-labelled, not
    deleted. stage_conf.json already refuses to gate the parallel L2-called-as-
    L3 case for exactly this reason.

Size alone cannot tell those apart. Your eyes can.

WHAT IT DOES
------------
Part A  Counts how many adult detections sit in the disputed band and what they
        score. If the band is nearly empty, stop here - the question is moot.

Part B  Crops the actual image regions and tiles them into two contact sheets:
          adult_labelled.png  - crops with size and score printed on them
          adult_blind.png     - the same crops, shuffled, small and large mixed,
                                with no labels, plus adult_blind_key.csv
        Use the BLIND sheet to decide. Looking only at small boxes while knowing
        they are the suspicious ones is how you talk yourself into seeing globs.
        Score the blind sheet worm/not-worm first, then read the key.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("cv2 not found - run this with the LAUNCHER venv "
             "(launcher\\.venv\\Scripts\\python.exe)")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_rows(csv_path: Path, target: str) -> tuple[list[dict], dict]:
    """Return (rows of the target class, {class: count} for everything)."""
    rows, per_class = [], defaultdict(int)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        needed = {"size_px", "hard_call", "image", "x1", "y1", "x2", "y2"}
        missing = needed - set(rd.fieldnames or [])
        if missing:
            sys.exit(f"{csv_path} is missing column(s): {sorted(missing)}\n"
                     "Is this a soft_stage_scores.csv from a Development run?")
        for r in rd:
            call = (r.get("hard_call") or "").strip()
            if not call:
                continue
            per_class[call] += 1
            if call.lower() == target.lower():
                try:
                    r["_size"] = float(r["size_px"])
                except (TypeError, ValueError):
                    continue
                try:
                    r["_score"] = float(r.get("hard_score") or "nan")
                except ValueError:
                    r["_score"] = float("nan")
                rows.append(r)
    return rows, dict(per_class)


def find_images(root: Path) -> dict:
    """basename -> path. Later duplicates are recorded so we can warn."""
    index, dupes = {}, set()
    exts = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
    for p in root.rglob("*"):
        if p.suffix.lower() in exts and p.is_file():
            if p.name in index:
                dupes.add(p.name)
            else:
                index[p.name] = p
    return index, dupes


def load_image(path: Path):
    """Read TIFF via tifffile when available, else cv2. Returns BGR uint8."""
    img = None
    if path.suffix.lower() in (".tif", ".tiff"):
        try:
            import tifffile
            img = tifffile.imread(str(path))
        except Exception:
            img = None
    if img is None:
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.dtype != np.uint8:                      # 16-bit -> 8-bit for display
        a = img.astype(np.float32)
        lo, hi = np.percentile(a, 0.5), np.percentile(a, 99.5)
        img = np.clip((a - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


# --------------------------------------------------------------------------
# Part A - the numbers
# --------------------------------------------------------------------------

def pct(n, d):
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def part_a(rows, per_class, target, lo, hi):
    print("=" * 72)
    print(f"PART A - where the '{target}' detections actually sit")
    print("=" * 72)

    total = sum(per_class.values())
    print(f"\n{total} detections in this run:")
    for c, n in sorted(per_class.items(), key=lambda kv: -kv[1]):
        print(f"    {c:14} {n:7}  {pct(n, total)}")

    if not rows:
        print(f"\nNo '{target}' detections. Nothing to decide.")
        return None

    sizes = np.array([r["_size"] for r in rows], dtype=float)
    print(f"\n'{target}' size (sqrt(w*h), px), n={len(sizes)}:")
    for q in (1, 5, 25, 50, 75, 95, 99):
        print(f"    p{q:<3} {np.percentile(sizes, q):7.1f}")
    print(f"    min  {sizes.min():7.1f}\n    max  {sizes.max():7.1f}")

    bands = [("below the shipped floor", -math.inf, lo),
             ("THE DISPUTED BAND", lo, hi),
             ("unambiguous", hi, math.inf)]
    print(f"\nBands (shipped floor {lo} px, L4 median {hi} px):")
    out = {}
    for name, a, b in bands:
        sel = [r for r in rows if a <= r["_size"] < b]
        out[name] = sel
        scores = np.array([r["_score"] for r in sel], dtype=float)
        scores = scores[np.isfinite(scores)]
        med = f"{np.median(scores):.3f}" if len(scores) else "n/a"
        rng = (f"{a:g}" if a > -math.inf else "0") + " - " + \
              (f"{b:g}" if b < math.inf else "inf")
        print(f"    {name:24} {rng:>12} px : {len(sel):6}  "
              f"{pct(len(sel), len(rows)):>7}   median score {med}")

    disputed = out["THE DISPUTED BAND"]
    share = 100.0 * len(disputed) / len(rows)
    print("\n--- what this means ---")
    if share < 5:
        print(f"  Only {share:.1f}% of '{target}' calls are in the disputed band.")
        print("  Raising the floor is low-risk whatever they turn out to be, and")
        print("  the debris problem is smaller than it felt. Still eyeball Part B.")
    elif share < 20:
        print(f"  {share:.1f}% of '{target}' calls are in the disputed band -")
        print("  enough to move your numbers. Part B decides which way.")
    else:
        print(f"  {share:.1f}% of '{target}' calls are in the disputed band.")
        print("  That is too many to delete on a hunch. If these are mislabelled")
        print("  worms rather than debris, a size gate here would remove a third")
        print("  of the class from BOTH sides of every ratio. Part B is not")
        print("  optional.")

    hi_conf = [r for r in disputed if r["_score"] >= 0.7]
    if disputed:
        print(f"\n  {len(hi_conf)} of {len(disputed)} disputed boxes score >= 0.70 "
              f"({pct(len(hi_conf), len(disputed))}).")
        print("  High scores here confirm the original diagnosis: the model is")
        print("  confident, so no confidence threshold can remove these.")
    return out


# --------------------------------------------------------------------------
# Part B - the contact sheets
# --------------------------------------------------------------------------

def crop(img, r, pad=0.35, size=200):
    h, w = img.shape[:2]
    x1, y1 = float(r["x1"]), float(r["y1"])
    x2, y2 = float(r["x2"]), float(r["y2"])
    bw, bh = x2 - x1, y2 - y1
    m = max(bw, bh) * (1 + pad)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    a, b = int(round(cx - m / 2)), int(round(cy - m / 2))
    a, b = max(0, min(a, w - 1)), max(0, min(b, h - 1))
    c, d = min(w, a + int(round(m))), min(h, b + int(round(m)))
    if c - a < 4 or d - b < 4:
        return None
    tile = cv2.resize(img[b:d, a:c], (size, size), interpolation=cv2.INTER_AREA)
    # Box outline in the crop's own coordinates. x and y are scaled separately:
    # a crop clipped by the image edge is not square, and resizing it to a
    # square stretches the axes by different factors. Using one scale for both
    # puts the outline off the worm exactly for the boxes near a plate edge.
    sx = size / max(c - a, 1)
    sy = size / max(d - b, 1)
    cv2.rectangle(tile,
                  (int((x1 - a) * sx), int((y1 - b) * sy)),
                  (int((x2 - a) * sx), int((y2 - b) * sy)), (0, 220, 0), 1)
    return tile


def sheet(tiles, labels, out_path, cols=6, size=200, pad=26):
    if not tiles:
        return False
    rows_n = math.ceil(len(tiles) / cols)
    canvas = np.full((rows_n * (size + pad), cols * size, 3), 245, np.uint8)
    for i, (t, lab) in enumerate(zip(tiles, labels)):
        r, c = divmod(i, cols)
        y, x = r * (size + pad), c * size
        canvas[y:y + size, x:x + size] = t
        cv2.putText(canvas, lab, (x + 4, y + size + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
    cv2.imwrite(str(out_path), canvas)
    return True


def part_b(bands, index, dupes, out_dir, target, n_each, seed):
    print("\n" + "=" * 72)
    print("PART B - look at them")
    print("=" * 72)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    disputed = bands["THE DISPUTED BAND"]
    control = bands["unambiguous"]
    if not disputed:
        print("\nNothing in the disputed band. No sheets written.")
        return
    pick_d = rng.sample(disputed, min(n_each, len(disputed)))
    pick_c = rng.sample(control, min(n_each, len(control)))
    print(f"\nSampling {len(pick_d)} disputed and {len(pick_c)} known-good "
          f"'{target}' boxes as a control.")
    if dupes:
        print(f"WARNING: {len(dupes)} image basename(s) appear more than once "
              "under --images; crops for those may come from the wrong copy.")

    cache, items = {}, []
    for r, tag in [(r, "disputed") for r in pick_d] + [(r, "control") for r in pick_c]:
        name = r["image"]
        p = index.get(name)
        if p is None:
            continue
        if p not in cache:
            cache[p] = load_image(p)
            if len(cache) > 40:                      # keep memory sane
                cache.pop(next(iter(cache)))
        img = cache.get(p)
        if img is None:
            continue
        t = crop(img, r)
        if t is not None:
            items.append({"tile": t, "tag": tag, "size": r["_size"],
                          "score": r["_score"], "image": name,
                          "det": r.get("det_index", "")})
    if not items:
        print("\nCould not crop anything. Is --images pointing at the folder "
              "that holds the original images?")
        return

    got_d = sum(1 for i in items if i["tag"] == "disputed")
    print(f"Cropped {len(items)} boxes ({got_d} disputed).")

    labelled = sorted(items, key=lambda i: i["size"])
    sheet([i["tile"] for i in labelled],
          [f'{i["size"]:.0f}px s={i["score"]:.2f}' for i in labelled],
          out_dir / "adult_labelled.png")

    blind = items[:]
    rng.shuffle(blind)
    sheet([i["tile"] for i in blind], [f"#{n + 1}" for n in range(len(blind))],
          out_dir / "adult_blind.png")
    with open(out_dir / "adult_blind_key.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["n", "band", "size_px", "hard_score", "image", "det_index"])
        for n, i in enumerate(blind, 1):
            w.writerow([n, i["tag"], f'{i["size"]:.1f}', f'{i["score"]:.4f}',
                        i["image"], i["det"]])

    print(f"\nWrote to {out_dir}:")
    print("    adult_blind.png      <- score this one FIRST, worm or not-worm")
    print("    adult_blind_key.csv  <- then read this")
    print("    adult_labelled.png   <- sorted by size, for reference after")
    print("\nHOW TO READ IT")
    print("  Go through adult_blind.png and write down which numbers are not")
    print("  worms. Then open the key.")
    print("    - not-worms concentrated in 'disputed'  -> debris. Raise the")
    print("      adult floor toward the L4 median.")
    print("    - disputed boxes are worms, just small   -> the model is")
    print("      mislabelling stage, NOT detecting debris. Do NOT gate: that")
    print("      deletes real animals from both sides of every ratio. Fix by")
    print("      relabelling on size, or retrain.")
    print("    - a mix                                  -> gating trades one")
    print("      error for another. Retrain with hard-negative debris crops.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, type=Path,
                    help="soft_stage_scores.csv from a Development run")
    ap.add_argument("--images", type=Path,
                    help="folder to search for the original images (Part B)")
    ap.add_argument("--out", type=Path, default=Path("adult_check"))
    ap.add_argument("--class", dest="target", default="adult")
    ap.add_argument("--floor", type=float, default=43.0,
                    help="shipped class_size_px lower bound (default 43)")
    ap.add_argument("--plausible", type=float, default=71.0,
                    help="size below which an 'adult' is implausible; the L4 "
                         "median (default 71)")
    ap.add_argument("--sample", type=int, default=24,
                    help="boxes per group on the contact sheets")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    if not a.csv.is_file():
        sys.exit(f"not found: {a.csv}")
    rows, per_class = read_rows(a.csv, a.target)
    bands = part_a(rows, per_class, a.target, a.floor, a.plausible)
    if bands is None:
        return
    if not a.images:
        print("\n(no --images given, so Part B was skipped - and Part B is the "
              "half that actually decides it)")
        return
    if not a.images.is_dir():
        sys.exit(f"not a folder: {a.images}")
    print(f"\nIndexing images under {a.images} ...")
    index, dupes = find_images(a.images)
    print(f"  {len(index)} image(s) found.")
    part_b(bands, index, dupes, a.out, a.target, a.sample, a.seed)


if __name__ == "__main__":
    main()
