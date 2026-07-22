#!/usr/bin/env python3
"""
infer_stage.py — staging inference CLI for the WormScan worm-survival pipeline.

Runs the YOLO staging model over C. elegans plate images using the shared
tiled_infer() (676x608 tiles, never a whole-frame resize — staging reads
absolute worm size, so resizing whole frames destroys the signal). The model is
loaded ONCE and reused for every image, so batch mode is cheap per image.

This is the ONLY place a model is loaded / a CLI is exposed. tiled_infer.py
stays a pure library — do NOT add a __main__ or model-loading there.

Runs ONLY under vision/.venv-vision (3.12, ultralytics + torch). The launcher's
3.13 side calls this as a subprocess and MUST NOT import it.

Modes
-----
single   python infer_stage.py IMAGE [--conf C] [--save-preview OUT.png]
                               [--draw OUT.png] [--counts OUT.txt]
             Emits one JSON object for the image to stdout. --draw/--counts are
             additive side outputs (annotated PNG + per-class counts txt); stdout
             is unchanged whether or not they are passed.

batch    python infer_stage.py --batch [ROOT] [--conf C] [--no-boxes]
                               [--stdin] [--preview-dir DIR]
             Discovers images under ROOT (recursive), OR with --stdin reads a
             newline-delimited list of image paths from stdin. Emits JSON Lines.

Batch stdout
------------
Line 1 (meta, no "path" key):
    {"names": ["egg","L1",...], "model": "<abs>", "conf": 0.25}
Then one object per image (always has "path"):
    {"path": "<abs>", "counts": {stage:int,...},
     "boxes": [[x1,y1,x2,y2,score,stage],...],   # omitted when --no-boxes
     "w": W, "h": H}
Per-image failure (processing continues):
    {"path": "<abs>", "error": "<msg>"}

The 3.13 consumer distinguishes the meta line (no "path") from image lines
("path" present). `names` is authoritative for the full stage list, so the
consumer never has to load the model to know every possible stage.

Progress/logs go to stderr only. Fatal errors (model missing, bad args) exit
non-zero; a single unreadable image does not abort a batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from tiled_infer import tiled_infer

# Discovery rules mirror the launcher's analysis/counting.py (and its
# counting_agent copy) so a folder walk here picks up exactly the same files
# the 3.13 side would resolve. Keep these in lockstep with that module.
_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3

_DEFAULT_MODEL = Path(__file__).parent / "models" / "staging.pt"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def find_images(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Recursively find image files up to ``max_depth`` levels deep, skipping
    dirs whose name starts with '_' (pipeline output) or '.' (hidden caches).
    Mirrors analysis/counting.py find_images so both sides agree on the set."""
    results: list[Path] = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in _IMAGE_EXTS:
                    results.append(child)
                elif child.is_dir() and not (
                    child.name.startswith("_") or child.name.startswith(".")
                ):
                    _recurse(child, depth + 1)
        except PermissionError:
            pass

    _recurse(folder, 1)
    return results


def load_model(model_path: Path):
    """Load the YOLO staging model once. Fatal (non-zero exit) if it fails."""
    from ultralytics import YOLO  # heavy import; only in the vision venv

    if not model_path.exists():
        _log(f"FATAL: model not found: {model_path}")
        raise SystemExit(2)
    _log(f"[infer] loading model: {model_path}")
    model = YOLO(str(model_path))
    _log(f"[infer] model loaded; classes: {list(model.names.values())}")
    return model


def infer_image(image_path: Path, model, names: dict, conf: float):
    """Run tiled inference on one image. Returns (counts, boxes, w, h).

    boxes: list of [x1, y1, x2, y2, score, stage] in full-frame coords.
    counts: {stage: n} over detected boxes (detected stages only; the meta
            line carries the authoritative full stage list).
    """
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    boxes = tiled_infer(img, model, names, conf=conf)
    counts: dict[str, int] = {}
    for b in boxes:
        stage = b[5]
        counts[stage] = counts.get(stage, 0) + 1
    return counts, boxes, w, h


# ---- preview rendering (optional; for spot-checking only) ------------------

_PREVIEW_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#9a6324",
]


def _color_for(stage: str) -> str:
    idx = int(hashlib.md5(stage.encode("utf-8")).hexdigest(), 16) % len(_PREVIEW_COLORS)
    return _PREVIEW_COLORS[idx]


def save_preview(image_path: Path, boxes: list, out_path: Path) -> None:
    """Draw detection boxes + stage labels on the image and save to out_path."""
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    for x1, y1, x2, y2, score, stage in boxes:
        color = _color_for(stage)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        draw.text((x1 + 2, max(0, y1 - 12)), f"{stage} {score:.2f}", fill=color)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def write_counts(counts: dict, names: dict, out_path: Path) -> None:
    """Write one line per class in model.names (0 when undetected) plus a total.
    `names` is authoritative for the full stage list, so every stage always
    appears even when the frame contains none of it."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    lines = []
    for stage in (names[i] for i in sorted(names)):
        n = int(counts.get(stage, 0))
        total += n
        lines.append(f"{stage}: {n}")
    lines.append(f"total: {total}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _preview_path(image_path: Path, preview_dir: Path) -> Path:
    """Collision-safe preview name: <stem>_<hash8>.png (paths across different
    plates can share a filename, so the abs-path hash disambiguates)."""
    h = hashlib.md5(str(image_path).encode("utf-8")).hexdigest()[:8]
    return preview_dir / f"{image_path.stem}_{h}.png"


# ---- modes -----------------------------------------------------------------

def run_single(args) -> int:
    model = load_model(Path(args.model))
    names = model.names
    image_path = Path(args.image).resolve()
    counts, boxes, w, h = infer_image(image_path, model, names, args.conf)
    if args.save_preview:
        save_preview(image_path, boxes, Path(args.save_preview))
        _log(f"[infer] preview -> {args.save_preview}")
    if args.draw:
        save_preview(image_path, boxes, Path(args.draw))
        _log(f"[infer] annotated -> {args.draw}")
    if args.counts:
        write_counts(counts, names, Path(args.counts))
        _log(f"[infer] counts -> {args.counts}")
    obj = {"path": str(image_path), "counts": counts, "w": w, "h": h}
    if not args.no_boxes:
        obj["boxes"] = boxes
    print(json.dumps(obj), flush=True)
    return 0


def run_batch(args) -> int:
    model = load_model(Path(args.model))
    names = model.names

    if args.stdin:
        images = [Path(line.strip()) for line in sys.stdin if line.strip()]
        _log(f"[infer] {len(images)} image path(s) from stdin")
    else:
        root = Path(args.root or ".").resolve()
        images = find_images(root)
        _log(f"[infer] {len(images)} image(s) discovered under {root}")

    preview_dir = Path(args.preview_dir).resolve() if args.preview_dir else None

    # Meta line first: authoritative stage list + run params for the consumer.
    print(json.dumps({
        "names": [names[i] for i in sorted(names)],
        "model": str(Path(args.model).resolve()),
        "conf": args.conf,
    }), flush=True)

    n = len(images)
    for i, image_path in enumerate(images, 1):
        image_path = image_path.resolve()
        _log(f"[infer] {i}/{n} {image_path.name}")
        try:
            counts, boxes, w, h = infer_image(image_path, model, names, args.conf)
        except Exception as exc:  # one bad image must not abort the batch
            _log(f"[infer] ERROR on {image_path}: {exc}")
            print(json.dumps({"path": str(image_path), "error": str(exc)[:300]}),
                  flush=True)
            continue
        if preview_dir is not None:
            try:
                save_preview(image_path, boxes, _preview_path(image_path, preview_dir))
            except Exception as exc:
                _log(f"[infer] preview failed for {image_path}: {exc}")
        obj = {"path": str(image_path), "counts": counts, "w": w, "h": h}
        if not args.no_boxes:
            obj["boxes"] = boxes
        print(json.dumps(obj), flush=True)

    _log(f"[infer] batch done: {n} image(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WormScan staging inference (tiled).")
    p.add_argument("image", nargs="?", help="image path (single mode)")
    p.add_argument("--batch", action="store_true", help="batch mode")
    p.add_argument("--root", help="batch: folder to walk recursively")
    p.add_argument("--stdin", action="store_true",
                   help="batch: read newline-delimited image paths from stdin")
    p.add_argument("--model", default=str(_DEFAULT_MODEL),
                   help="path to staging.pt")
    p.add_argument("--conf", type=float, default=0.25,
                   help="per-tile confidence threshold (default 0.25)")
    p.add_argument("--no-boxes", action="store_true",
                   help="omit per-box lists from JSON (smaller output)")
    p.add_argument("--preview-dir",
                   help="batch: draw boxes and save a preview PNG per image here")
    p.add_argument("--save-preview",
                   help="single: draw boxes and save a preview PNG to this path")
    p.add_argument("--draw",
                   help="single: draw boxes and save an annotated PNG to this path")
    p.add_argument("--counts",
                   help="single: write a per-class counts txt to this path")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch:
        if not args.stdin and not args.root:
            _log("FATAL: batch mode needs --root or --stdin")
            return 2
        return run_batch(args)
    if not args.image:
        _log("FATAL: single mode needs an IMAGE path (or use --batch)")
        return 2
    return run_single(args)


if __name__ == "__main__":
    raise SystemExit(main())
