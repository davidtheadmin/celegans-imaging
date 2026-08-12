#!/usr/bin/env python3
"""
tiled_assist.py - pre-annotate full frames with the staging model, then push the
boxes to Roboflow so you *correct* them instead of drawing from scratch.

Runs in the VISION venv (3.12, ultralytics + torch):

    launcher\\vision\\.venv-vision\\Scripts\\python.exe ^
        dev\\tools\\tiled_assist.py "C:\\...\\Train_gravidAdult_upload"

WHAT CHANGED (2026-07-27)
------------------------
This used to live loose in Documents\\WormScan and `sys.path`-hack an import of a
`tiled_infer.py` sitting next to it. That copy no longer exists, so the script
was simply broken. It now imports launcher/vision/tiled_infer.py — the same
module the launcher and the Analyze-on-laptop button use — so assist boxes get
every merge cleanup the counting path gets:

  * per-class confidence floors      (from stage_conf.json)
  * seam-fragment suppression        (one worm, one box across a tile seam)
  * cross-class NMS                  (one worm carrying two stage labels)
  * per-class size gate              (debris scoring high on 'adult')

WHY FULL FRAMES ARE UPLOADED, NOT TILES
---------------------------------------
4056/6 = 676 and 3040/5 = 608 exactly: the tile size IS Roboflow's 6x5 tiling
preprocessing. Roboflow tiles at dataset-generation time, so the upload must be
the whole frame with whole-frame annotation coordinates. Do not "helpfully"
upload tiles — you would tile twice and halve the effective worm size.

Inference re-tiles at that same 676x608 but WITH overlap, so it runs more tiles
than 6x5 (8x7 at overlap 0.2, 9x8 at 0.35). That is deliberate: overlap is what
stops a worm being sliced by every seam that touches it.

EGGS ARE ANNOTATED BY DEFAULT HERE
----------------------------------
stage_conf.json ships `exclude_classes: ["egg"]` because a *counting* run rarely
wants eggs. Annotation is the opposite job: a pre-annotation that silently omits
eggs teaches the next model that eggs are background, which is worse than not
pre-annotating at all. So this script clears the exclusion unless you ask for it
with --exclude-classes.

WORKFLOW
--------
  1. prep_roboflow.py <trainset> --go     ->  <trainset>_upload/*.png
  2. tiled_assist.py  <trainset>_upload   ->  _assist/*.xml + *_preview.png
  3. eyeball the previews
  4. tiled_assist.py  <trainset>_upload --upload --workspace W --project P

Phase 2 needs `pip install roboflow` and ROBOFLOW_API_KEY (or --api-key).
Delete blank frames in Roboflow FIRST or the upload is skipped as a duplicate.

Parity with the pre-2026-07-27 behaviour, if you ever need to compare:
    --conf 0.15 --no-seam-suppress --no-size-gate --class-agnostic-iou 0.5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The one true tiling/merge implementation. Resolved from this file's location
# so the script works from any cwd and there is no second copy to drift.
_REPO = Path(__file__).resolve().parents[2]
_VISION = _REPO / "launcher" / "vision"
sys.path.insert(0, str(_VISION))

from tiled_infer import resolve_class_conf, tiled_infer  # noqa: E402

_STAGE_CONF = _VISION / "stage_conf.json"
_DEFAULT_WEIGHTS = _VISION / "models" / "staging.pt"

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

# Fixed colours for the stages we know; anything else gets a stable hashed
# colour rather than all-unknowns sharing one yellow (a retrain that adds a
# class should be visible in the preview, not camouflaged).
CLASS_COLORS = {
    "egg":         (255, 0, 255),
    "L1":          (0, 200, 255),
    "L2":          (0, 255, 120),
    "L3":          (255, 220, 0),
    "L4":          (255, 120, 0),
    "young adult": (170, 90, 255),
    "adult":       (255, 0, 0),
}
_FALLBACK_COLORS = [(0, 255, 200), (255, 160, 160), (120, 255, 0), (255, 255, 255)]


def color_for(cls: str):
    if cls in CLASS_COLORS:
        return CLASS_COLORS[cls]
    h = int(hashlib.md5(cls.encode("utf-8")).hexdigest(), 16)
    return _FALLBACK_COLORS[h % len(_FALLBACK_COLORS)]


def load_stage_conf() -> dict:
    try:
        raw = json.loads(_STAGE_CONF.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! could not read {_STAGE_CONF} ({exc}); using bare defaults")
        return {}


# ---------- VOC ----------

def write_voc(xml_path, img_name, w, h, dets):
    """Pascal VOC, full-frame pixel coords. Roboflow reads this directly."""
    ann = ET.Element("annotation")
    ET.SubElement(ann, "filename").text = img_name
    size = ET.SubElement(ann, "size")
    ET.SubElement(size, "width").text = str(w)
    ET.SubElement(size, "height").text = str(h)
    ET.SubElement(size, "depth").text = "3"
    for x1, y1, x2, y2, _, cls in dets:
        o = ET.SubElement(ann, "object")
        ET.SubElement(o, "name").text = str(cls)
        ET.SubElement(o, "difficult").text = "0"
        bb = ET.SubElement(o, "bndbox")
        ET.SubElement(bb, "xmin").text = str(int(round(x1)))
        ET.SubElement(bb, "ymin").text = str(int(round(y1)))
        ET.SubElement(bb, "xmax").text = str(int(round(x2)))
        ET.SubElement(bb, "ymax").text = str(int(round(y2)))
    ET.ElementTree(ann).write(xml_path, encoding="utf-8", xml_declaration=True)


# ---------- inference ----------

def run_infer(opts, model, names, img_path, out_dir):
    im = Image.open(img_path).convert("RGB")
    W, H = im.size

    dets = tiled_infer(
        im, model, names,
        tile_w=opts["tile_w"], tile_h=opts["tile_h"], overlap=opts["overlap"],
        conf=opts["floor"], iou=opts["iou"],
        class_conf=opts["class_conf"],
        class_size_px=opts["class_size_px"],
        exclude_classes=opts["exclude_classes"],
        seam_margin=opts["seam_margin"],
        seam_cover_frac=opts["seam_cover_frac"],
        class_agnostic_iou=opts["class_agnostic_iou"],
        same_class_cover_frac=opts["same_class_cover_frac"],
    )

    stem = img_path.stem
    write_voc(str(out_dir / f"{stem}.xml"), img_path.name, W, H, dets)

    prev = im.copy()
    d = ImageDraw.Draw(prev)
    try:
        font = ImageFont.truetype("arial.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    for x1, y1, x2, y2, sc, cls in dets:
        c = color_for(cls)
        d.rectangle([x1, y1, x2, y2], outline=c, width=3)
        d.text((x1, max(0, y1 - 32)), f"{cls} {sc:.2f}", fill=c, font=font)
    prev.save(out_dir / f"{stem}_preview.png")

    per_class = Counter(det[5] for det in dets)
    summary = " ".join(f"{k}:{v}" for k, v in sorted(per_class.items())) or "none"
    print(f"  {img_path.name}: {len(dets):3d} boxes   {summary}")
    return per_class


# ---------- upload ----------

def run_upload(args, images, out_dir):
    from roboflow import Roboflow
    key = args.api_key or os.environ.get("ROBOFLOW_API_KEY")
    if not key:
        sys.exit("Upload needs ROBOFLOW_API_KEY or --api-key.")
    project = Roboflow(api_key=key).workspace(args.workspace).project(args.project)
    batch = args.batch_name or "tiled_assist"

    # Local record of what already uploaded, so a resumed run skips them without
    # re-hitting the server. One filename per line.
    done_log = out_dir / "_uploaded.txt"
    done = set()
    if done_log.exists():
        done = set(done_log.read_text(encoding="utf-8").splitlines())

    n = skipped = failed = 0
    with open(done_log, "a", encoding="utf-8") as log:
        for img in images:
            if img.name in done:
                skipped += 1
                continue
            xml = out_dir / f"{img.stem}.xml"
            if not xml.exists():
                print(f"  no xml for {img.name}, skipped")
                continue
            try:
                try:
                    project.single_upload(image_path=str(img), annotation_path=str(xml),
                                          batch_name=batch, is_prediction=True)
                except TypeError:
                    project.upload(str(img), annotation_path=str(xml),
                                   batch_name=batch, is_prediction=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                failed += 1
                print(f"  FAILED {img.name}: {e}")
                continue
            log.write(img.name + "\n")
            log.flush()
            n += 1
            print(f"  uploaded {img.name}")

    print(f"\nUploaded {n} new, skipped {skipped} already done, {failed} failed.")
    if failed:
        print("Re-run the same command to retry the failed ones "
              "(successful ones are logged and skipped).")


# ---------- options ----------

def resolve_opts(args) -> dict:
    """stage_conf.json defaults, with CLI overrides layered on top."""
    cfg = load_stage_conf()

    class_conf = {k: v for k, v in (cfg.get("class_conf") or {}).items()}
    if args.conf is not None:
        # Uniform override: the recall-biased mode the old script defaulted to.
        class_conf = {"_default": float(args.conf)}
    floor = min([float(v) for v in class_conf.values()] or [0.15])

    tiling = cfg.get("tiling") or {}
    seam = cfg.get("seam") or {}
    merge = cfg.get("merge") or {}

    overlap = args.overlap if args.overlap is not None else tiling.get("overlap", 0.2)
    margin = args.seam_margin if args.seam_margin is not None else seam.get("margin_px", 0)
    cover = (args.seam_cover_frac if args.seam_cover_frac is not None
             else seam.get("cover_frac"))
    if args.no_seam_suppress:
        cover = None
    ca = (args.class_agnostic_iou if args.class_agnostic_iou is not None
          else merge.get("class_agnostic_iou"))
    if ca is not None and ca >= 1.0:
        ca = None
    scn = (args.same_class_cover_frac if args.same_class_cover_frac is not None
           else merge.get("same_class_cover_frac"))
    if args.no_same_class_nesting:
        scn = None
    size = {k: v for k, v in (cfg.get("class_size_px") or {}).items()
            if not str(k).startswith("_")}
    if args.no_size_gate:
        size = {}

    # Annotation wants every class. stage_conf.json's exclusion is a COUNTING
    # default; inheriting it here would ship pre-annotations with no egg boxes.
    excluded = ([c.strip() for c in args.exclude_classes.split(",") if c.strip()]
                if args.exclude_classes else [])

    return {
        "class_conf": class_conf, "floor": floor,
        "tile_w": args.tile_w, "tile_h": args.tile_h, "overlap": float(overlap),
        "iou": args.iou,
        "seam_margin": int(margin or 0),
        "seam_cover_frac": None if cover is None else float(cover),
        "class_agnostic_iou": None if ca is None else float(ca),
        "same_class_cover_frac": None if scn is None else float(scn),
        "class_size_px": size,
        "exclude_classes": excluded,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="+",
                    help="Folder(s) of full frames (usually <trainset>_upload). "
                         "Several may be given; the model is loaded once for all "
                         "of them, and each gets its own _assist/ subfolder.")
    ap.add_argument("--weights", default=str(_DEFAULT_WEIGHTS),
                    help="YOLO .pt (default: launcher/vision/models/staging.pt).")
    ap.add_argument("--out", help="Output folder (default: <input>/_assist).")
    ap.add_argument("--conf", type=float, default=None,
                    help="Uniform confidence floor for EVERY class, overriding the "
                         "per-class values in stage_conf.json. 0.15 reproduces the "
                         "old recall-biased behaviour.")
    ap.add_argument("--iou", type=float, default=0.45,
                    help="Per-class NMS IoU for merging tile overlaps.")
    ap.add_argument("--tile-w", type=int, default=676)
    ap.add_argument("--tile-h", type=int, default=608)
    ap.add_argument("--overlap", type=float, default=None,
                    help="Default: from stage_conf.json (0.35).")
    ap.add_argument("--seam-margin", type=int, default=None)
    ap.add_argument("--seam-cover-frac", type=float, default=None)
    ap.add_argument("--no-seam-suppress", action="store_true")
    ap.add_argument("--class-agnostic-iou", type=float, default=None,
                    help="Cross-class NMS. Default: from stage_conf.json (0.7). "
                         "1.0 disables.")
    ap.add_argument("--same-class-cover-frac", type=float, default=None)
    ap.add_argument("--no-same-class-nesting", action="store_true",
                    help="keep nested same-class boxes (partial + whole worm).")
    ap.add_argument("--no-size-gate", action="store_true")
    ap.add_argument("--exclude-classes", default=None,
                    help="Comma-separated classes to leave unannotated. Default: "
                         "NONE — annotation wants every class, unlike counting.")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--api-key")
    ap.add_argument("--workspace")
    ap.add_argument("--project")
    ap.add_argument("--batch-name")
    args = ap.parse_args()

    in_dirs = [Path(x) for x in args.input]
    for d in in_dirs:
        if not d.is_dir():
            sys.exit(f"Not a folder: {d}")
    if args.out and len(in_dirs) > 1:
        sys.exit("--out applies to a single input folder; run those one at a time.")

    # Resolve every folder's images up front so a typo in the last path fails
    # before the model spends minutes on the first one.
    jobs = []
    for d in in_dirs:
        out_dir = Path(args.out) if args.out else d / "_assist"
        imgs = sorted(p for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() in IMG_EXTS)
        if not imgs:
            sys.exit(f"No images found in {d}\n"
                     f"  (is it the <trainset>_upload folder, not the raw trainset?)")
        jobs.append((d, out_dir, imgs))

    if args.upload:
        if not (args.workspace and args.project):
            sys.exit("--upload needs --workspace and --project.")
        for d, out_dir, images in jobs:
            print(f"\n=== {d.name}: uploading {len(images)} frames + annotations ===")
            out_dir.mkdir(parents=True, exist_ok=True)
            run_upload(args, images, out_dir)
        return

    opts = resolve_opts(args)

    from ultralytics import YOLO
    print(f"Loading {args.weights} ...")
    model = YOLO(args.weights)   # once, for every folder
    names = model.names if isinstance(model.names, dict) else dict(enumerate(model.names))

    eff = resolve_class_conf(opts["class_conf"], names, fallback=0.15)
    nframes = sum(len(j[2]) for j in jobs)
    print(f"\nfolders           : {len(jobs)}")
    print(f"frames            : {nframes}")
    print(f"tile              : {opts['tile_w']}x{opts['tile_h']} "
          f"@ overlap {opts['overlap']}  (Roboflow tiles the upload 6x5 itself)")
    print(f"conf per class    : " + ", ".join(f"{k}={v:.2f}" for k, v in eff.items()))
    print(f"seam suppression  : margin {opts['seam_margin']} px, "
          f"cover {opts['seam_cover_frac']}")
    print(f"cross-class NMS   : {opts['class_agnostic_iou']}")
    print(f"nested same-class : {opts['same_class_cover_frac']}")
    print(f"size gate         : " + (", ".join(f"{k}={list(v)}"
          for k, v in opts["class_size_px"].items()) or "off (not measured)"))
    print(f"not annotated     : " + (", ".join(opts["exclude_classes"])
          or "(nothing — every class is annotated)"))
    print()

    grand = Counter()
    for d, out_dir, images in jobs:
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {d.name}: {len(images)} frame(s) ===")
        total = Counter()
        for img in images:
            total.update(run_infer(opts, model, names, img, out_dir))
        grand.update(total)
        line = "  ".join(f"{k} {v}" for k, v in sorted(total.items())) or "nothing detected"
        print(f"  -> {sum(total.values())} boxes   {line}")
        print(f"  -> {out_dir}")

    if len(jobs) > 1:
        print(f"\nAll folders: {sum(grand.values())} boxes")
        for cls, n in sorted(grand.items()):
            print(f"    {cls:>12} {n}")
    if not grand:
        print("    (nothing detected — check --conf and the weights path)")
    print("\nCheck *_preview.png. Then delete blank frames in Roboflow and re-run")
    print("with --upload --workspace .. --project .. to correct them there.")


if __name__ == "__main__":
    main()
