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

Defaults
--------
Every knob below defaults to whatever `stage_conf.json` (next to this file)
says: the per-class confidence floors, the tile overlap, and the seam
duplicate-suppression params. That file is the single source of truth shared
with the launcher, which is what makes the Worm Survival batch run and the
capture web UI's "Analyze on laptop" button agree without either side
hard-coding numbers. Command-line flags override it; missing/unreadable file
falls back to the historical uniform 0.25 with the seam passes off.

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

Threshold / merge flags (both modes)
    --class-conf JSON      inline {"L2": 0.4, ...}; merged over stage_conf.json
    --conf C               uniform floor for EVERY class; overrides both of those
    --class-size-px JSON   inline {"adult": [120, 400], ...}; sqrt(w*h) bounds
    --exclude-classes A,B  drop these classes entirely (default: egg)
    --count-eggs           shorthand: keep eggs, i.e. clear "egg" from the above
    --overlap F            tile overlap (0.0-0.9)
    --seam-margin PX / --seam-cover-frac F / --no-seam-suppress
    --class-agnostic-iou F / --no-class-agnostic
    --no-size-gate
    --print-config         resolve everything, print it as JSON, exit (no model)
    --rescore-alpha F      per-class score rescoring; 0 = off (exact no-op).
                           Relabels detections, never adds or removes one.
    --soft-csv PATH        side output: one row per detection carrying the full
                           per-class score vector. Purely additive — the boxes,
                           counts and stdout are byte-identical with or without
                           it. Scores are per-class sigmoids (they do NOT sum to
                           1) and are uncalibrated.

Batch stdout
------------
Line 1 (meta, no "path" key):
    {"names": ["egg","L1",...], "model": "<abs>", "conf": 0.25,
     "class_conf": {"egg":0.3,...}, "overlap": 0.35,
     "seam": {"margin_px": 12, "cover_frac": 0.6},
     "class_agnostic_iou": 0.7, "class_size_px": {...},
     "exclude_classes": ["egg"]}
Then one object per image (always has "path"):
    {"path": "<abs>", "counts": {stage:int,...},
     "boxes": [[x1,y1,x2,y2,score,stage],...],   # omitted when --no-boxes
     "w": W, "h": H}
Per-image failure (processing continues):
    {"path": "<abs>", "error": "<msg>"}

"conf" on the meta line is the effective floor actually handed to the model
(the minimum across classes); "class_conf" is the authoritative per-class map.
The 3.13 consumer distinguishes the meta line (no "path") from image lines
("path" present). `names` is authoritative for the full stage list, so the
consumer never has to load the model to know every possible stage.

Progress/logs go to stderr only. Fatal errors (model missing, bad args) exit
non-zero; a single unreadable image does not abort a batch.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from tiled_infer import resolve_class_conf, tiled_infer

# Discovery rules mirror the launcher's analysis/counting.py (and its
# counting_agent copy) so a folder walk here picks up exactly the same files
# the 3.13 side would resolve. Keep these in lockstep with that module.
_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3

_DEFAULT_MODEL = Path(__file__).parent / "models" / "staging.pt"
_DEFAULT_STAGE_CONF = Path(__file__).parent / "stage_conf.json"

# Used only when stage_conf.json is missing or unreadable: the historical
# behaviour (uniform 0.25, original tiling, both seam passes off).
_FALLBACK_CONF = 0.25
_FALLBACK_OVERLAP = 0.2


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---- shared defaults -------------------------------------------------------

def load_stage_conf(path: Path) -> dict:
    """Read stage_conf.json. Never fatal: a missing or malformed file logs and
    yields the historical defaults, because an inference run that silently
    stops working is worse than one that runs with old numbers and says so."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log(f"[infer] no stage config at {path}; using uniform {_FALLBACK_CONF}")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"[infer] WARNING: could not read {path} ({exc}); "
             f"using uniform {_FALLBACK_CONF}")
        return {}
    return raw if isinstance(raw, dict) else {}


def resolve_options(args) -> dict:
    """Merge stage_conf.json with the command-line overrides.

    Precedence, loosest to tightest: file defaults < --class-conf < --conf.
    --conf is deliberately the bluntest instrument: passing it pins EVERY class
    to one number, which is what the pre-per-class callers meant by it.
    """
    cfg = load_stage_conf(Path(args.stage_conf))

    class_conf = dict(cfg.get("class_conf") or {})
    if not class_conf:
        class_conf = {"_default": _FALLBACK_CONF}

    if args.class_conf:
        try:
            override = json.loads(args.class_conf)
        except json.JSONDecodeError as exc:
            _log(f"FATAL: --class-conf is not valid JSON: {exc}")
            raise SystemExit(2)
        if not isinstance(override, dict):
            _log("FATAL: --class-conf must be a JSON object")
            raise SystemExit(2)
        class_conf.update({k: float(v) for k, v in override.items()})

    if args.conf is not None:
        class_conf = {"_default": float(args.conf)}

    tiling = cfg.get("tiling") or {}
    overlap = args.overlap if args.overlap is not None else tiling.get(
        "overlap", _FALLBACK_OVERLAP)

    seam = cfg.get("seam") or {}
    margin = args.seam_margin if args.seam_margin is not None else seam.get(
        "margin_px", 0)
    cover = (args.seam_cover_frac if args.seam_cover_frac is not None
             else seam.get("cover_frac"))
    if args.no_seam_suppress:
        cover = None

    merge = cfg.get("merge") or {}
    ca_iou = (args.class_agnostic_iou if args.class_agnostic_iou is not None
              else merge.get("class_agnostic_iou"))
    if args.no_class_agnostic:
        ca_iou = None

    scn = (args.same_class_cover_frac if args.same_class_cover_frac is not None
           else merge.get("same_class_cover_frac"))
    if args.no_same_class_nesting:
        scn = None

    class_size = {k: v for k, v in (cfg.get("class_size_px") or {}).items()
                  if not str(k).startswith("_")}
    if args.class_size_px:
        try:
            override = json.loads(args.class_size_px)
        except json.JSONDecodeError as exc:
            _log(f"FATAL: --class-size-px is not valid JSON: {exc}")
            raise SystemExit(2)
        if not isinstance(override, dict):
            _log("FATAL: --class-size-px must be a JSON object")
            raise SystemExit(2)
        class_size.update(override)
    if args.no_size_gate:
        class_size = {}

    # An excluded class is not a class that scored zero. Every consumer of this
    # list has to say "not counted" rather than "0", or a plate full of eggs
    # reads as a plate with no eggs.
    excluded = list(cfg.get("exclude_classes") or [])
    if args.exclude_classes is not None:
        excluded = [c.strip() for c in args.exclude_classes.split(",") if c.strip()]
    if args.count_eggs:
        excluded = [c for c in excluded if c.strip().lower() != "egg"]

    rescore = dict(cfg.get("rescore") or {})
    rescore.pop("_README", None)
    if args.rescore_alpha is not None:
        rescore["alpha"] = float(args.rescore_alpha)
    rescore["alpha"] = float(rescore.get("alpha") or 0.0)
    rescore["refs"] = {k: float(v) for k, v in (rescore.get("refs") or {}).items()
                       if not str(k).startswith("_")}

    return {
        "class_conf": class_conf,
        "rescore": rescore,
        "overlap": float(overlap),
        "seam_margin": int(margin or 0),
        "seam_cover_frac": None if cover is None else float(cover),
        "class_agnostic_iou": None if ca_iou is None else float(ca_iou),
        "same_class_cover_frac": None if scn is None else float(scn),
        "class_size_px": class_size,
        "exclude_classes": excluded,
    }


def effective_floor(class_conf: dict) -> float:
    """The lowest per-class threshold — what the model itself must run at, or
    detections below it never come back to be filtered."""
    vals = [float(v) for v in class_conf.values()]
    return min(vals) if vals else _FALLBACK_CONF


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


def infer_image(image_path: Path, model, names: dict, opts: dict,
                collect_scores: bool = False):
    # tiled_infer turns collect_scores on itself when rescoring, but say so here
    # too so the returned tuples are the width this function documents.
    if float((opts.get("rescore") or {}).get("alpha") or 0.0):
        collect_scores = True
    """Run tiled inference on one image. Returns (counts, boxes, w, h).

    boxes: list of [x1, y1, x2, y2, score, stage] in full-frame coords, or of
           [x1, y1, x2, y2, score, stage, score_vector, match_iou] when
           collect_scores is set. Callers that only want the 6-tuple can slice.
    counts: {stage: n} over detected boxes (detected stages only; the meta
            line carries the authoritative full stage list).
    """
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    boxes = tiled_infer(
        img, model, names,
        overlap=opts["overlap"],
        conf=effective_floor(opts["class_conf"]),
        class_conf=opts["class_conf"],
        seam_margin=opts["seam_margin"],
        seam_cover_frac=opts["seam_cover_frac"],
        class_agnostic_iou=opts["class_agnostic_iou"],
        same_class_cover_frac=opts["same_class_cover_frac"],
        class_size_px=opts["class_size_px"],
        exclude_classes=opts["exclude_classes"],
        collect_scores=collect_scores,
        rescore=opts.get("rescore"),
    )
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
    for box in boxes:
        # slice: boxes carry two extra members when soft scores are collected
        x1, y1, x2, y2, score, stage = box[:6]
        color = _color_for(stage)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        draw.text((x1 + 2, max(0, y1 - 12)), f"{stage} {score:.2f}", fill=color)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def write_counts(counts: dict, names: dict, out_path: Path,
                 excluded: list | None = None) -> None:
    """Write one line per class in model.names (0 when undetected) plus a total.
    `names` is authoritative for the full stage list, so every stage always
    appears even when the frame contains none of it.

    An excluded class prints "not counted", never "0" — those are different
    claims, and printing 0 for a class we never looked for would read as
    "this plate has no eggs" on a plate that might be covered in them.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    skip = {str(c).strip().lower() for c in (excluded or [])}
    total = 0
    lines = []
    for stage in (names[i] for i in sorted(names)):
        if stage.strip().lower() in skip:
            lines.append(f"{stage}: not counted")
            continue
        n = int(counts.get(stage, 0))
        total += n
        lines.append(f"{stage}: {n}")
    lines.append(f"total: {total}")
    if skip:
        lines.append(f"(excluded from this run: {', '.join(sorted(skip))})")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- soft per-class scores (optional side output) --------------------------

class SoftScoreWriter:
    """Write one CSV row per detection carrying its full per-class vector.

    This is a pure side output. It is fed from the detections the normal
    pipeline already decided to keep, so the counts, the Excel and every
    suppression pass are bit-for-bit what they would be without it.

    Columns
    -------
    image, det_index, x1..y2, w_px, h_px, size_px   geometry (full-frame px)
    hard_call, hard_score                           what the pipeline counted
    hard_call_raw                                   the label BEFORE per-class
                                                    rescoring. Equal to hard_call
                                                    when rescoring is off, so the
                                                    two columns differing is
                                                    exactly the set of animals the
                                                    pass moved.
    match_iou                                       raw-candidate match quality;
                                                    ~1.0 is expected, low values
                                                    mean the vector may belong to
                                                    a different box — filter on it
    entropy                                         of the normalised vector, in
                                                    nats; high = model unsure
    raw_<class>                                     per-class SIGMOID score, as
                                                    the model emits it. These do
                                                    NOT sum to 1.
    p_<class>                                       raw_ divided by their sum. A
                                                    normalisation WE chose, not a
                                                    calibrated probability — do
                                                    not report as a % without
                                                    calibrating against hand
                                                    counts first.

    Every class in the model appears, including classes excluded from counting:
    an excluded class never produces a box, but a kept box still carries its
    score for that class, and that is often the interesting part.
    """

    def __init__(self, path: Path, names: dict):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stages = [str(names[i]) for i in sorted(names)]
        self.n_rows = 0
        self.n_missing = 0
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        self._w.writerow(
            ["image", "det_index", "x1", "y1", "x2", "y2",
             "w_px", "h_px", "size_px", "hard_call", "hard_call_raw",
             "hard_score", "match_iou", "entropy"]
            + [f"raw_{s}" for s in self.stages]
            + [f"p_{s}" for s in self.stages]
        )

    def add_image(self, image_path: Path, boxes: list) -> None:
        for i, box in enumerate(boxes):
            x1, y1, x2, y2, score, stage = box[:6]
            vec = box[6] if len(box) > 6 else None
            miou = box[7] if len(box) > 7 else float("nan")
            stage_raw = box[8] if len(box) > 8 else stage
            w = max(0.0, float(x2) - float(x1))
            h = max(0.0, float(y2) - float(y1))
            row = [image_path.name, i,
                   round(float(x1), 2), round(float(y1), 2),
                   round(float(x2), 2), round(float(y2), 2),
                   round(w, 2), round(h, 2), round(math.sqrt(w * h), 2),
                   stage, stage_raw, round(float(score), 5)]
            if not vec or len(vec) != len(self.stages):
                # No vector for this box: record the row anyway so the CSV and
                # the counts always have the same number of animals, and the
                # gap is visible rather than silently dropped.
                self.n_missing += 1
                row += ["", ""] + [""] * (2 * len(self.stages))
                self._w.writerow(row)
                self.n_rows += 1
                continue
            total = sum(float(v) for v in vec) or 1.0
            probs = [float(v) / total for v in vec]
            ent = -sum(p * math.log(p) for p in probs if p > 0)
            row += [("" if miou != miou else round(float(miou), 4)),
                    round(ent, 5)]
            row += [round(float(v), 6) for v in vec]
            row += [round(p, 6) for p in probs]
            self._w.writerow(row)
            self.n_rows += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _preview_path(image_path: Path, preview_dir: Path) -> Path:
    """Collision-safe preview name: <stem>_<hash8>.png (paths across different
    plates can share a filename, so the abs-path hash disambiguates)."""
    h = hashlib.md5(str(image_path).encode("utf-8")).hexdigest()[:8]
    return preview_dir / f"{image_path.stem}_{h}.png"


def _log_options(opts: dict, resolved: dict) -> None:
    pretty = ", ".join(f"{k}={v:.2f}" for k, v in resolved.items())
    _log(f"[infer] class conf: {pretty}")
    _log(f"[infer] overlap={opts['overlap']} seam_margin={opts['seam_margin']} "
         f"seam_cover_frac={opts['seam_cover_frac']} "
         f"class_agnostic_iou={opts['class_agnostic_iou']} "
         f"same_class_cover_frac={opts['same_class_cover_frac']}")
    sizes = opts["class_size_px"]
    _log("[infer] size gate: " + (
        ", ".join(f"{k}={list(v)}" for k, v in sizes.items()) if sizes
        else "off (no class_size_px measured — see stage_conf.json)"))
    rs = opts.get("rescore") or {}
    if float(rs.get("alpha") or 0.0):
        _log(f"[infer] RESCORING ON: alpha={rs['alpha']} refs=" + ", ".join(
            f"{k}={v:.4f}" for k, v in (rs.get("refs") or {}).items())
            + "  (relabels only — detection count is unchanged)")
    else:
        _log("[infer] rescoring: off (alpha 0)")
    _log("[infer] excluded classes: "
         + (", ".join(opts["exclude_classes"]) or "(none)"))


# ---- modes -----------------------------------------------------------------

def run_single(args, opts: dict) -> int:
    model = load_model(Path(args.model))
    names = model.names
    resolved = resolve_class_conf(opts["class_conf"], names,
                                  fallback=_FALLBACK_CONF)
    _log_options(opts, resolved)
    image_path = Path(args.image).resolve()
    soft = SoftScoreWriter(Path(args.soft_csv), names) if args.soft_csv else None
    counts, boxes, w, h = infer_image(image_path, model, names, opts,
                                      collect_scores=soft is not None)
    if soft is not None:
        soft.add_image(image_path, boxes)
        soft.close()
        _log(f"[infer] soft scores -> {args.soft_csv} "
             f"({soft.n_rows} row(s), {soft.n_missing} without a vector)")
    if args.save_preview:
        save_preview(image_path, boxes, Path(args.save_preview))
        _log(f"[infer] preview -> {args.save_preview}")
    if args.draw:
        save_preview(image_path, boxes, Path(args.draw))
        _log(f"[infer] annotated -> {args.draw}")
    if args.counts:
        write_counts(counts, names, Path(args.counts), opts["exclude_classes"])
        _log(f"[infer] counts -> {args.counts}")
    obj = {"path": str(image_path), "counts": counts, "w": w, "h": h}
    if not args.no_boxes:
        obj["boxes"] = [list(b[:6]) for b in boxes]   # stdout contract: 6-tuples
    print(json.dumps(obj), flush=True)
    return 0


def run_batch(args, opts: dict) -> int:
    model = load_model(Path(args.model))
    names = model.names
    resolved = resolve_class_conf(opts["class_conf"], names,
                                  fallback=_FALLBACK_CONF)
    _log_options(opts, resolved)

    if args.stdin:
        images = [Path(line.strip()) for line in sys.stdin if line.strip()]
        _log(f"[infer] {len(images)} image path(s) from stdin")
    else:
        root = Path(args.root or ".").resolve()
        images = find_images(root)
        _log(f"[infer] {len(images)} image(s) discovered under {root}")

    preview_dir = Path(args.preview_dir).resolve() if args.preview_dir else None
    soft = SoftScoreWriter(Path(args.soft_csv), names) if args.soft_csv else None
    if soft is not None:
        _log(f"[infer] soft per-class scores -> {soft.path}")

    # Meta line first: authoritative stage list + run params for the consumer.
    # class_conf is spelled with the model's own class names so the launcher can
    # copy it straight into run_info without re-resolving aliases.
    print(json.dumps({
        "names": [names[i] for i in sorted(names)],
        "model": str(Path(args.model).resolve()),
        "conf": effective_floor(opts["class_conf"]),
        "class_conf": resolved,
        "overlap": opts["overlap"],
        "seam": {"margin_px": opts["seam_margin"],
                 "cover_frac": opts["seam_cover_frac"]},
        "class_agnostic_iou": opts["class_agnostic_iou"],
        "same_class_cover_frac": opts["same_class_cover_frac"],
        "class_size_px": opts["class_size_px"],
        "exclude_classes": opts["exclude_classes"],
        "rescore": opts.get("rescore"),
    }), flush=True)

    n = len(images)
    for i, image_path in enumerate(images, 1):
        image_path = image_path.resolve()
        _log(f"[infer] {i}/{n} {image_path.name}")
        try:
            counts, boxes, w, h = infer_image(image_path, model, names, opts,
                                              collect_scores=soft is not None)
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
        if soft is not None:
            try:
                soft.add_image(image_path, boxes)
            except Exception as exc:   # a side output must never fail the run
                _log(f"[infer] soft-score row failed for {image_path}: {exc}")
        obj = {"path": str(image_path), "counts": counts, "w": w, "h": h}
        if not args.no_boxes:
            obj["boxes"] = [list(b[:6]) for b in boxes]  # stdout stays 6-tuples
        print(json.dumps(obj), flush=True)

    if soft is not None:
        soft.close()
        _log(f"[infer] soft scores written: {soft.n_rows} detection(s), "
             f"{soft.n_missing} without a vector -> {soft.path}")

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
    p.add_argument("--stage-conf", default=str(_DEFAULT_STAGE_CONF),
                   help="shared defaults JSON (default: stage_conf.json here)")
    p.add_argument("--class-conf",
                   help='inline per-class thresholds, e.g. \'{"L2":0.4}\'; '
                        "merged over stage_conf.json")
    p.add_argument("--conf", type=float, default=None,
                   help="uniform confidence floor for every class; overrides "
                        "stage_conf.json and --class-conf entirely")
    p.add_argument("--overlap", type=float, default=None,
                   help="tile overlap fraction (default: from stage_conf.json)")
    p.add_argument("--seam-margin", type=int, default=None,
                   help="px from an interior tile seam that flags a detection "
                        "as a possible fragment (0 disables)")
    p.add_argument("--seam-cover-frac", type=float, default=None,
                   help="drop a flagged fragment when this fraction of it is "
                        "inside a higher-scoring box")
    p.add_argument("--no-seam-suppress", action="store_true",
                   help="keep seam fragments (disables the duplicate fix)")
    p.add_argument("--class-agnostic-iou", type=float, default=None,
                   help="extra cross-class NMS threshold, for one worm carrying "
                        "two labels at nearly the same box")
    p.add_argument("--no-class-agnostic", action="store_true",
                   help="disable the cross-class NMS pass")
    p.add_argument("--same-class-cover-frac", type=float, default=None,
                   help="drop a box this far inside a LARGER box of the same "
                        "class (nested partial detections)")
    p.add_argument("--no-same-class-nesting", action="store_true",
                   help="disable the nested same-class suppression pass")
    p.add_argument("--class-size-px",
                   help='inline per-class size bounds, e.g. '
                        '\'{"adult":[120,400]}\'; sqrt(w*h) in full-frame px, '
                        "merged over stage_conf.json")
    p.add_argument("--no-size-gate", action="store_true",
                   help="disable the per-class size plausibility gate")
    p.add_argument("--exclude-classes",
                   help="comma-separated class names to drop entirely, e.g. "
                        "'egg'; empty string keeps everything")
    p.add_argument("--count-eggs", action="store_true",
                   help="keep egg detections (clears 'egg' from the exclusion "
                        "list, however it was set)")
    p.add_argument("--print-config", action="store_true",
                   help="print the resolved settings as JSON and exit; does "
                        "not load the model")
    p.add_argument("--rescore-alpha", type=float, default=None,
                   help="per-class score rescoring strength; 0 = off (exact "
                        "no-op), 1 = full division by each class's reference "
                        "score. Relabels only; never changes the box count.")
    p.add_argument("--soft-csv",
                   help="write one CSV row per detection with its full "
                        "per-class score vector (side output; does not change "
                        "any detection or count)")
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
    opts = resolve_options(args)
    if args.print_config:
        print(json.dumps(opts, indent=2), flush=True)
        return 0
    if args.batch:
        if not args.stdin and not args.root:
            _log("FATAL: batch mode needs --root or --stdin")
            return 2
        return run_batch(args, opts)
    if not args.image:
        _log("FATAL: single mode needs an IMAGE path (or use --batch)")
        return 2
    return run_single(args, opts)


if __name__ == "__main__":
    raise SystemExit(main())
