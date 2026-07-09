"""
worm_stage_preview.py  --  first-pass worm segmentation + length-based stage guess.

Throwaway diagnostic. NOT part of the WormScan app and NOT a YOLO/Roboflow exporter
yet. Its only job is to let you SEE whether segmentation + length measurement are
sane before we build anything on top.

Point it at a folder of stills. For every image it:
  1. segments the worms,
  2. estimates each worm's length (ribbon model: from area + perimeter, so it
     survives bending without needing a skeleton library),
  3. converts length to microns using the calibration you set below,
  4. guesses a stage from the length,
  5. writes "<name>_preview.png" next to the image with boxes + length + stage,
  6. dumps "measurements.csv" with every worm's length so you can recalibrate the
     stage bins from your synchronized plates.

Run it, open a couple of the _preview.png files, and tell me what it gets wrong.

Usage:
    python worm_stage_preview.py
    (or:  python worm_stage_preview.py  "C:\\path\\to\\folder")
"""

import csv
import math
import sys
from pathlib import Path

import cv2
import numpy as np

# ----------------------------------------------------------------------------
# KNOBS -- these are the things you'll tweak. Start here.
# ----------------------------------------------------------------------------

INPUT_DIR = r"C:\Users\Isabe\Documents\WormScan\Counting_test_images"

# Your calibration. You said it's baked into the TIFFs; the script will try to
# read it (see read_um_per_px) and PRINT what it found. But the safe path for a
# first run is to just put your known value here -- it overrides metadata.
# Set to None to trust the metadata instead.
MANUAL_UM_PER_PX = 5.05

# Darkfield = bright worms on a dark background -> True.
# Brightfield = dark worms on a bright background -> False.
IS_DARKFIELD = True

# Ignore blobs smaller/larger than this many pixels (debris below, clumps above).
# These are in PIXELS, not microns. Tune after the first look.
MIN_AREA_PX = 200
MAX_AREA_PX = 200000

# Segmentation. We use a "top-hat" filter: it keeps bright thin structures
# (worms) and subtracts the slowly-varying background (lawn, vignetting). This
# is what lets it grab in-focus worms while ignoring the lawn carpet/texture.
#   TOPHAT_KERNEL_PX  ~ a bit larger than a worm is WIDE. Bigger = catch fatter
#                       worms but also more background. Start ~31, raise for
#                       bigger/fatter worms.
#   TOPHAT_PERCENTILE = how bright a pixel must be (within the filtered image) to
#                       count. Higher = stricter = fewer detections. 99.0 grabs
#                       only the clear worms; lower it (98, 97) to catch fainter
#                       ones at the cost of more junk.
TOPHAT_KERNEL_PX = 31
TOPHAT_PERCENTILE = 99.0

# Stage length bins in MICRONS. Textbook ~20C starting values -- REPLACE these
# with the ranges you measure off your synchronized L1/L2/... plates.
# (name, min_um, max_um); first match wins, checked in order.
STAGE_BINS = [
    ("L1",    200, 300),
    ("L2",    300, 430),
    ("L3",    430, 600),
    ("L4",    600, 820),
    ("adult", 820, 1500),
]

# Worms whose length lands within this fraction of a bin edge get flagged "?"
# so you know to look at them rather than trusting the auto label.
BOUNDARY_FRAC = 0.10

# Round, short blobs are likely eggs rather than larvae. Crude first rule.
EGG_MAX_LENGTH_UM = 70
EGG_MIN_CIRCULARITY = 0.6      # 1.0 = perfect circle

# Debris filter -- drops colony dots, bubbles, and merged clumps so they don't
# get labelled as worms. Three rules:
#   1. round + not egg-sized            -> colony dot / bubble
#   2. very solid AND round (even if it got a length) -> compact junk, not a worm
#      (real worms are thin and curvy = LOW solidity; debris is near-solid ~1.0)
#   3. length absurdly long             -> two+ worms merged into one blob
DEBRIS_CIRCULARITY_MIN = 0.75  # above this + no real elongation = debris
DEBRIS_SOLIDITY_MIN = 0.85     # worms sit well below this; discs sit near 1.0
CLUMP_MAX_LENGTH_UM = 1500     # longer than a big adult = merged worms
HIDE_DEBRIS = False            # True = don't draw debris boxes at all (just count)

# ----------------------------------------------------------------------------

VALID_EXT = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
STAGE_COLORS = {  # BGR
    "egg": (0, 200, 200), "L1": (0, 220, 0), "L2": (0, 180, 255),
    "L3": (255, 120, 0), "L4": (255, 0, 200), "adult": (0, 0, 230),
    "?": (160, 160, 160), "none": (120, 120, 120), "debris": (90, 90, 90),
}


def read_um_per_px(path):
    """Best-effort read of microns/pixel from TIFF metadata. Returns (val, note)."""
    if path.suffix.lower() not in {".tif", ".tiff"}:
        return None, "not a tiff"
    try:
        import tifffile
    except ImportError:
        return None, "tifffile not installed (pip install tifffile)"
    try:
        with tifffile.TiffFile(str(path)) as tif:
            page = tif.pages[0]
            ij = tif.imagej_metadata or {}
            tags = page.tags
            xres = tags.get("XResolution")
            unit = ij.get("unit", "")
            if xres is not None:
                v = xres.value
                xr = v[0] / v[1] if isinstance(v, tuple) else float(v)
                if xr > 0:
                    # ImageJ stores pixels-per-unit; um/px = 1/xr if unit is um.
                    if unit in ("micron", "um", "\xb5m", "microns"):
                        return 1.0 / xr, f"imagej unit={unit}, XRes={xr:.4f}"
                    return 1.0 / xr, f"XRes={xr:.4f}, unit='{unit}' (CHECK units!)"
        return None, "no resolution tag found"
    except Exception as e:  # noqa
        return None, f"read error: {e}"


def to_gray_u8(img):
    """Handle 16-bit and colour inputs -> single-channel 8-bit."""
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.dtype != np.uint8:
        img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return img


def segment(gray):
    """Return a binary mask of worm pixels (255) and the contours.

    Top-hat filter isolates bright thin worms from the slowly-varying lawn:
    morphological opening estimates the background, top-hat = image - opening,
    so only structures thinner than the kernel survive. We then threshold at a
    high percentile so the faint lawn carpet stays out."""
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    if not IS_DARKFIELD:
        blur = cv2.bitwise_not(blur)   # make worms bright either way
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (TOPHAT_KERNEL_PX, TOPHAT_KERNEL_PX))
    th = cv2.morphologyEx(blur, cv2.MORPH_TOPHAT, k)
    cutoff = np.percentile(th, TOPHAT_PERCENTILE)
    mask = (th > cutoff).astype(np.uint8) * 255
    sk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, sk, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, sk, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return mask, contours


def ribbon_length_px(area, perim):
    """Length of a worm modelled as a thin ribbon: area=L*W, perim=2(L+W).
    -> L = (P + sqrt(P^2 - 16A)) / 4.  Survives curling because it uses area,
    not a straight-line span. disc<0 means 'not elongated' (round-ish)."""
    disc = perim * perim - 16.0 * area
    if disc < 0:
        return None
    return (perim + math.sqrt(disc)) / 4.0


def classify(length_um, equiv_diam_um, circularity, solidity):
    # Round / non-elongated blob: no ribbon length.
    if length_um is None:
        if (equiv_diam_um is not None and equiv_diam_um <= EGG_MAX_LENGTH_UM
                and circularity >= EGG_MIN_CIRCULARITY):
            return "egg", False
        # round, not egg-sized -> colony dot / bubble / junk
        return "debris", False
    # Short + round even with a length -> still likely an egg.
    if length_um <= EGG_MAX_LENGTH_UM and circularity >= EGG_MIN_CIRCULARITY:
        return "egg", False
    # Merged blob far longer than any worm -> clump.
    if length_um > CLUMP_MAX_LENGTH_UM:
        return "debris", False
    # Got a length but is near-solid AND round -> compact junk, not a thin worm.
    if (solidity is not None and solidity >= DEBRIS_SOLIDITY_MIN
            and circularity >= DEBRIS_CIRCULARITY_MIN):
        return "debris", False
    for name, lo, hi in STAGE_BINS:
        if lo <= length_um < hi:
            near = (abs(length_um - lo) < BOUNDARY_FRAC * (hi - lo)
                    or abs(length_um - hi) < BOUNDARY_FRAC * (hi - lo))
            return name, near
    return "?", True


def process(path, um_per_px, rows):
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        print(f"  !! could not read {path.name}")
        return
    gray = to_gray_u8(img)
    vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    _, contours = segment(gray)

    counts = {}
    n = 0
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA_PX or area > MAX_AREA_PX:
            continue
        perim = cv2.arcLength(c, True)
        circ = 4 * math.pi * area / (perim * perim) if perim > 0 else 0.0
        hull_area = cv2.contourArea(cv2.convexHull(c))
        solidity = area / hull_area if hull_area > 0 else 1.0
        L_px = ribbon_length_px(area, perim)
        L_um = L_px * um_per_px if (L_px and um_per_px) else None
        equiv_diam_um = (2 * math.sqrt(area / math.pi) * um_per_px
                         if um_per_px else None)
        stage, near = classify(L_um, equiv_diam_um, circ, solidity)
        label = ("?" if near else stage)
        n += 1
        counts[stage] = counts.get(stage, 0) + 1

        rows.append({
            "file": path.name, "worm": n, "area_px": round(area, 1),
            "perim_px": round(perim, 1), "circularity": round(circ, 3),
            "solidity": round(solidity, 3),
            "length_px": round(L_px, 1) if L_px else "",
            "length_um": round(L_um, 1) if L_um else "",
            "equiv_diam_um": round(equiv_diam_um, 1) if equiv_diam_um else "",
            "stage_guess": stage, "near_boundary": near,
        })

        if stage == "debris" and HIDE_DEBRIS:
            continue
        x, y, w, h = cv2.boundingRect(c)
        color = STAGE_COLORS.get(stage, (200, 200, 200))
        cv2.rectangle(vis, (x, y), (x + w, y + h), color, 2)
        if stage == "debris":
            txt = ""  # boxed but unlabelled to reduce clutter
        elif stage == "egg":
            txt = f"egg {equiv_diam_um:.0f}um" if equiv_diam_um else "egg"
        elif L_um:
            txt = f"{label} {L_um:.0f}um"
        else:
            txt = f"{label}"
        if txt:
            cv2.putText(vis, txt, (x, max(0, y - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    out = path.with_name(path.stem + "_preview.png")
    cv2.imwrite(str(out), vis)
    n_debris = counts.get("debris", 0)
    n_worms = n - n_debris
    summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())
                        if k != "debris") or "nothing"
    print(f"  {path.name}: {n_worms} worms ({summary}) "
          f"+ {n_debris} debris dropped  ->  {out.name}")


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else INPUT_DIR)
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        return
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in VALID_EXT
                    and not p.stem.endswith("_preview"))
    if not images:
        print(f"No images found in {folder}")
        return

    print(f"Found {len(images)} image(s) in {folder}\n")
    rows = []
    for p in images:
        meta_um, note = read_um_per_px(p)
        um_per_px = MANUAL_UM_PER_PX if MANUAL_UM_PER_PX else meta_um
        src = ("MANUAL override" if MANUAL_UM_PER_PX
               else f"metadata ({note})" if meta_um else f"NONE -- {note}")
        print(f"{p.name}: um/px = {um_per_px}  [{src}]")
        if not um_per_px:
            print("    -> no calibration; lengths will be blank. "
                  "Set MANUAL_UM_PER_PX at the top.")
        process(p, um_per_px, rows)

    if rows:
        csv_path = folder / "measurements.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} measurements -> {csv_path}")
    print("\nDone. Open the *_preview.png files and check the boxes + lengths.")


if __name__ == "__main__":
    main()
