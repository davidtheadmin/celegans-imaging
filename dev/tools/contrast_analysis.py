#!/usr/bin/env python3
"""
Contrast analysis for WormScan images.

Walks a folder recursively, computes contrast metrics per file, and
summarizes per containing folder (typically a condition or plate
subfolder in the WormScan mirror layout).

Usage:
    python contrast_analysis.py <folder>
    python contrast_analysis.py <folder> --csv out.csv
    python contrast_analysis.py <folder> --group-depth 2

Supports .jpg/.jpeg/.png/.tif/.tiff/.bmp and .mp4/.avi/.mov (middle frame).

Metrics per image:
    std       - std of grayscale pixel intensities (RMS contrast)
    sat_pct   - % of pixels >= 254 (saturation; below ~0.5% is good)
    range     - P99 - P1 intensity (effective dynamic range; ~240 is ideal)
    fg_frac   - % of pixels above Otsu threshold (foreground fraction)
    fg_std    - std of foreground (above-Otsu) pixels: how much worm-internal
                detail is preserved. This is the headline metric.

Higher fg_std with sat_pct < 0.5% is the best exposure.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

import cv2
import numpy as np

IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}


def load_gray(path: Path):
    """Load an image, or for a video, return the middle frame as grayscale."""
    suffix = path.suffix.lower()
    if suffix in IMG_EXTS:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return img
    if suffix in VIDEO_EXTS:
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return None


def compute_metrics(gray: np.ndarray) -> dict:
    flat = gray.ravel()
    std = float(flat.std())
    sat_pct = float((flat >= 254).sum() / flat.size * 100)
    p1 = float(np.percentile(flat, 1))
    p99 = float(np.percentile(flat, 99))
    rng = p99 - p1

    otsu_thresh, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    fg_mask = flat > otsu_thresh
    fg = flat[fg_mask]
    fg_frac = float(fg.size / flat.size * 100)
    fg_std = float(fg.std()) if fg.size > 1 else 0.0

    return {
        "std": std,
        "sat_pct": sat_pct,
        "p1": p1,
        "p99": p99,
        "range": rng,
        "otsu_thresh": float(otsu_thresh),
        "fg_frac": fg_frac,
        "fg_std": fg_std,
    }


def group_name_for(path: Path, root: Path, depth: int) -> str:
    """Return the name of the folder `depth` levels above the file.
    depth=1 = immediate parent, depth=2 = grandparent, etc."""
    parts = path.relative_to(root).parts
    idx = -1 - depth  # parts[-1] is filename
    if abs(idx) > len(parts):
        return "(root)"
    return parts[idx]


def main():
    ap = argparse.ArgumentParser(
        description="Contrast metrics for a folder of WormScan images/videos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("folder", help="Root folder to scan recursively")
    ap.add_argument("--csv", help="Optional CSV output path for per-image rows")
    ap.add_argument(
        "--group-depth",
        type=int,
        default=1,
        help="Aggregate by Nth-parent folder name (1=parent, 2=grandparent). "
        "For the WormScan mirror layout pointed at an experiment folder, "
        "use 2 to group by condition; use 1 to group by plate.",
    )
    args = ap.parse_args()

    root = Path(args.folder).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    files = [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in (IMG_EXTS | VIDEO_EXTS)
    ]
    if not files:
        print(f"No images or videos found under {root}", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {len(files)} files under {root} (group-depth={args.group_depth})")

    results = []  # list of (path, metrics, group)
    for f in files:
        gray = load_gray(f)
        if gray is None:
            print(f"  skip (unreadable): {f.relative_to(root)}", file=sys.stderr)
            continue
        m = compute_metrics(gray)
        g = group_name_for(f, root, args.group_depth)
        results.append((f, m, g))

    if not results:
        print("No readable files.", file=sys.stderr)
        sys.exit(1)

    # Per-image table
    print()
    print("=" * 105)
    print(
        f"{'file':<55} {'std':>7} {'sat%':>6} {'range':>6} {'fg%':>6} {'fg_std':>8}"
    )
    print("-" * 105)
    for path, m, _ in results:
        rel = str(path.relative_to(root))
        if len(rel) > 55:
            rel = "..." + rel[-52:]
        print(
            f"{rel:<55} {m['std']:>7.1f} {m['sat_pct']:>6.2f} "
            f"{m['range']:>6.0f} {m['fg_frac']:>6.1f} {m['fg_std']:>8.1f}"
        )

    # Per-group summary
    groups = defaultdict(list)
    for _, m, g in results:
        groups[g].append(m)

    summary = []
    for name, ms in groups.items():
        avg = {k: mean(m[k] for m in ms) for k in ms[0].keys()}
        summary.append((name, len(ms), avg))
    summary.sort(key=lambda x: x[2]["fg_std"], reverse=True)

    print()
    print("=" * 105)
    print(f"GROUP SUMMARY (mean across {len(results)} files, sorted by fg_std)")
    print("=" * 105)
    print(
        f"{'group':<35} {'n':>3} {'std':>7} {'sat%':>6} {'range':>6} {'fg%':>6} {'fg_std':>8}"
    )
    print("-" * 105)
    for name, n, avg in summary:
        if len(name) > 35:
            name = name[:32] + "..."
        print(
            f"{name:<35} {n:>3} {avg['std']:>7.1f} {avg['sat_pct']:>6.2f} "
            f"{avg['range']:>6.0f} {avg['fg_frac']:>6.1f} {avg['fg_std']:>8.1f}"
        )

    print()
    best_fg = summary[0]
    best_overall = max(summary, key=lambda x: x[2]["std"])
    least_sat = min(summary, key=lambda x: x[2]["sat_pct"])
    print(f"Best fg_std (worm detail):  {best_fg[0]}  ({best_fg[2]['fg_std']:.1f})")
    print(
        f"Best std (overall spread):  {best_overall[0]}  ({best_overall[2]['std']:.1f})"
    )
    print(
        f"Least saturation:           {least_sat[0]}  ({least_sat[2]['sat_pct']:.2f}%)"
    )

    # Optional warning if anyone's saturating badly
    bad = [s for s in summary if s[2]["sat_pct"] > 0.5]
    if bad:
        print()
        print(
            f"Warning: {len(bad)} group(s) with sat% > 0.5 — those exposures "
            "are clipping worm detail:"
        )
        for name, _, avg in bad:
            print(f"  {name}: {avg['sat_pct']:.2f}%")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "path",
                    "group",
                    "std",
                    "sat_pct",
                    "p1",
                    "p99",
                    "range",
                    "otsu_thresh",
                    "fg_frac",
                    "fg_std",
                ]
            )
            for path, m, g in results:
                w.writerow(
                    [
                        str(path.relative_to(root)),
                        g,
                        f"{m['std']:.2f}",
                        f"{m['sat_pct']:.3f}",
                        f"{m['p1']:.0f}",
                        f"{m['p99']:.0f}",
                        f"{m['range']:.0f}",
                        f"{m['otsu_thresh']:.0f}",
                        f"{m['fg_frac']:.2f}",
                        f"{m['fg_std']:.2f}",
                    ]
                )
        print(f"\nWrote {args.csv}")


if __name__ == "__main__":
    main()