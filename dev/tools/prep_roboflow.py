#!/usr/bin/env python3
"""
prep_roboflow.py - convert WormScan captures to 8-bit PNGs with metadata filenames.

Expected layout:
    <root>/                       e.g. Trainset_L1   (stage is taken from this name)
        <strain> <cohort>/        e.g. "N2 A", "601 A", "XPF A"
            plate 01/             plate NN (any digits in the name)
                *.tif / *.png ... 0..N images (searched recursively)

Output: a flat folder of PNGs named
    {strain}_{stage}_{cohort}_p{NN}_{NNNN}.png    e.g. N2_L1_A_p01_0001.png
plus manifest.csv mapping every PNG back to its source path + dtype + shape.

Dry-run by default. It prints what it would do. Add --go to actually write.

    python prep_roboflow.py                                  # folder picker, dry-run
    python prep_roboflow.py "C:\\...\\Trainset_L1"           # dry-run
    python prep_roboflow.py "C:\\...\\Trainset_L1" --go      # write PNGs
    python prep_roboflow.py "C:\\...\\Trainset_L1" --go --u16-max 4095   # 12-bit source

Deps: tifffile, numpy, Pillow. Reads via tifffile/PIL (RGB-native), writes via PIL.
No BGR swap happens anywhere.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

IMG_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}


# ---------- parsing ----------

def parse_stage(root: Path, override):
    """Stage label from the folder name, e.g. Trainset_L1 -> L1.

    The prefix regex accepts 'Train', 'Trainset' and 'Trainingset' because the
    real folders use both conventions (Trainset_L1 but Train_gravidAdult). The
    older 'trainset'-only pattern silently left 'Train_gravidAdult' as the stage,
    which then went into every output filename.
    """
    if override:
        return override
    name = re.sub(r"(?i)^train(ing)?(set)?[_\-\s]+", "", root.name).strip()
    return name or root.name


def parse_condition(folder_name: str):
    """'N2 A' -> ('N2','A'); 'HAL 601 A' -> ('HAL601','A'); '601 A' -> ('601','A')."""
    parts = folder_name.split()
    if len(parts) < 2:
        return None, None
    cohort = re.sub(r"[^A-Za-z0-9]", "", parts[-1])
    strain = re.sub(r"[^A-Za-z0-9]", "", "".join(parts[:-1]))
    if not strain or not cohort:
        return None, None
    return strain, cohort


def parse_plate(folder_name: str):
    m = re.search(r"(\d+)", folder_name)
    return f"p{int(m.group(1)):02d}" if m else None


# ---------- image IO ----------

def peek(path: Path):
    """Cheap dtype + shape for the report. No full pixel decode for TIFFs."""
    try:
        if path.suffix.lower() in (".tif", ".tiff"):
            with tifffile.TiffFile(str(path)) as tf:
                s = tf.series[0]
                return str(s.dtype), tuple(int(x) for x in s.shape)
        with Image.open(path) as im:
            return im.mode, (im.height, im.width)
    except Exception as e:
        return f"ERR:{type(e).__name__}", ()


def load_array(path: Path):
    if path.suffix.lower() in (".tif", ".tiff"):
        return tifffile.imread(str(path))
    with Image.open(path) as im:
        im.load()
        return np.array(im)


def to_uint8(arr, u16_max):
    """Return an 8-bit array (gray or RGB). uint8 passes through untouched."""
    arr = np.squeeze(arr)
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4):
        arr = np.moveaxis(arr, 0, -1)          # channels-first -> channels-last
    if arr.ndim == 3 and arr.shape[-1] == 4:
        arr = arr[..., :3]                     # drop alpha

    if arr.dtype == np.uint8:
        return arr
    if arr.dtype == np.uint16:
        if u16_max <= 255:
            return arr.astype(np.uint8)
        return np.clip(arr.astype(np.float32) * (255.0 / u16_max), 0, 255).astype(np.uint8)

    # float / other: per-image min-max (last resort, flagged in report)
    a = arr.astype(np.float32)
    lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
    if hi <= lo:
        return np.zeros(a.shape, np.uint8)
    return (((a - lo) / (hi - lo)) * 255).astype(np.uint8)


# ---------- walk ----------

def _subdirs(path: Path, issues: list):
    """Sorted subdirectories, skipping ones we cannot read.

    Windows scatters permission-denied junctions around a drive root (the German
    locale's '\\Dokumente und Einstellungen' is one), and a single one of those
    used to abort the entire run with a PermissionError traceback. A folder we
    cannot open is a warning, not a crash.
    """
    try:
        return sorted(p for p in path.iterdir() if p.is_dir())
    except (PermissionError, OSError) as exc:
        issues.append(f"skip '{path}': {type(exc).__name__}")
        return []


def collect(root: Path):
    records, issues = [], []
    for cond in _subdirs(root, issues):
        strain, cohort = parse_condition(cond.name)
        if not strain:
            issues.append(f"skip condition '{cond.name}': cannot parse strain/cohort")
            continue
        for plate_dir in _subdirs(cond, issues):
            plate = parse_plate(plate_dir.name)
            if not plate:
                issues.append(f"skip plate '{cond.name}/{plate_dir.name}': no number in name")
                continue
            try:
                imgs = sorted(
                    p for p in plate_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in IMG_EXTS
                )
            except (PermissionError, OSError) as exc:
                issues.append(f"skip '{plate_dir}': {type(exc).__name__}")
                continue
            for i, img in enumerate(imgs, start=1):
                records.append({"strain": strain, "cohort": cohort,
                                "plate": plate, "idx": i, "src": img})
    return records, issues


def newname(rec, stage):
    return f"{rec['strain']}_{stage}_{rec['cohort']}_{rec['plate']}_{rec['idx']:04d}.png"


# ---------- main ----------

def pick_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk(); r.withdraw()
        d = filedialog.askdirectory(title="Select the trainset folder")
        r.destroy()
        return Path(d) if d else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Rename+convert WormScan captures to PNG for Roboflow.")
    ap.add_argument("root", nargs="*", help="Trainset folder(s). Opens a picker if omitted. "
                                            "Several may be given; each gets its own <root>_upload.")
    ap.add_argument("--stage", help="Override stage label (default: parsed from root folder name).")
    ap.add_argument("--out", help="Output folder (default: <root>_upload).")
    ap.add_argument("--go", action="store_true", help="Actually write files (default is dry-run).")
    ap.add_argument("--overwrite", action="store_true", help="Allow writing into a non-empty output folder.")
    ap.add_argument("--u16-max", type=int, default=None,
                    help="16-bit scaling max (default: auto = global max across the set; e.g. 4095 for 12-bit).")
    args = ap.parse_args()

    roots = [Path(r) for r in args.root] if args.root else None
    if not roots:
        picked = pick_folder()
        roots = [picked] if picked else []
    if not roots:
        sys.exit("No folder selected.")
    if len(roots) > 1 and (args.stage or args.out):
        sys.exit("--stage/--out apply to a single root; run them one at a time.")
    for r in roots:
        if not r.is_dir():
            sys.exit(f"Not a folder: {r}")
        # A bare "\" on PowerShell (which does NOT treat \ as a line
        # continuation) resolves to the drive root, and the walk below would
        # then crawl the entire disk looking for "<strain> <cohort>/plate NN".
        # Refuse rather than spend minutes discovering that by accident.
        if r.resolve() == Path(r.resolve().anchor):
            sys.exit(
                f"Refusing to run on the drive root ({r.resolve()}).\n"
                "  This usually means a line continuation was taken as an "
                "argument:\n"
                "  PowerShell continues lines with a backtick ` , not with \\ .\n"
                "  Easiest fix: put the whole command on one line."
            )

    rc = 0
    for r in roots:
        if len(roots) > 1:
            print("\n" + "=" * 70)
        rc |= process(r, args)
    sys.exit(rc)


def process(root: Path, args) -> int:
    stage = parse_stage(root, args.stage)
    out = Path(args.out) if args.out else root.parent / f"{root.name}_upload"

    records, issues = collect(root)

    print(f"\nroot   : {root}")
    print(f"stage  : {stage}")
    print(f"out    : {out}")
    print(f"images : {len(records)}")

    # per-condition counts
    counts = {}
    for r in records:
        k = f"{r['strain']} {r['cohort']}"
        counts[k] = counts.get(k, 0) + 1
    for k in sorted(counts):
        print(f"    {k:12s} {counts[k]}")

    # dtype histogram (cheap peek)
    dtypes = {}
    for r in records:
        d, _ = peek(r["src"])
        dtypes[d] = dtypes.get(d, 0) + 1
    print("dtypes :", ", ".join(f"{d}x{n}" for d, n in sorted(dtypes.items())))

    if issues:
        print("\nWARNINGS:")
        for w in issues:
            print("   ", w)

    print("\nsample names:")
    for r in records[:5]:
        print(f"    {r['src'].name}  ->  {newname(r, stage)}")

    if not records:
        print("\nNothing to do.")
        return 1

    if not args.go:
        print("\nDRY RUN. Re-run with --go to write.")
        return 0

    # ----- write -----
    if out.exists() and any(out.iterdir()) and not args.overwrite:
        print(f"\nOutput folder not empty: {out}\nUse --overwrite or pick a fresh --out.")
        return 1
    out.mkdir(parents=True, exist_ok=True)

    # resolve 16-bit scaling
    u16_max = args.u16_max
    if u16_max is None:
        gmax = 0
        for r in records:
            if r["src"].suffix.lower() in (".tif", ".tiff"):
                a = load_array(r["src"])
                if a.dtype == np.uint16:
                    gmax = max(gmax, int(a.max()))
        u16_max = gmax if gmax > 0 else 255
        if gmax > 255:
            print(f"\n16-bit source detected. Scaling by global max = {u16_max} "
                  f"(pass --u16-max to pin, e.g. 4095 for 12-bit).")

    manifest = out / "manifest.csv"
    written, seen = 0, set()
    with open(manifest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["png", "strain", "stage", "cohort", "plate",
                    "orig_dtype", "orig_shape", "source_path"])
        for r in records:
            name = newname(r, stage)
            if name in seen:
                print(f"   COLLISION, skipped: {name}")
                continue
            seen.add(name)
            try:
                arr = load_array(r["src"])
                d, shp = str(getattr(arr, "dtype", "")), tuple(arr.shape)
                Image.fromarray(to_uint8(arr, u16_max)).save(out / name, format="PNG")
                w.writerow([name, r["strain"], stage, r["cohort"], r["plate"],
                            d, shp, str(r["src"])])
                written += 1
            except Exception as e:
                print(f"   FAILED {r['src']}: {type(e).__name__}: {e}")

    print(f"\nWrote {written} PNGs to {out}")
    print(f"Manifest: {manifest}")
    print("\nNext: pre-annotate with the staging model, then correct in Roboflow:")
    print(f'    launcher\\vision\\.venv-vision\\Scripts\\python.exe '
          f'dev\\tools\\tiled_assist.py "{out}"')
    return 0


if __name__ == "__main__":
    main()
