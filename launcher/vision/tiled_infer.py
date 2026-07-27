#!/usr/bin/env python3
"""
tiled_infer.py - shared tiled inference for the WormScan staging pipeline.

Single implementation of: tile a full 4056x3040 frame at 676x608, run a YOLO
staging model on each tile, shift boxes back to full-frame coords, merge.

Everything that needs tiled detection (assist labeling, conf histograms,
per-class thresholds, the launcher snapshot button) should import tiled_infer()
from here so the tiling params and NMS logic live in exactly one place. If any
caller drifts from the 676x608 tile resized to 640, staging accuracy degrades
invisibly, so don't. `overlap` is the one tiling knob that is safe to change:
it shifts where the seams fall without changing the pixels-per-object scale the
model sees, which is the thing staging actually reads.

Geometry note (why the seam machinery below exists). With tile 676x608 and
overlap 0.2 the step is 541x486, so the frame is cut into 8x7 = 56 tiles and
adjacent tiles share only 135 px in x / 122 px in y. A detection is guaranteed
to sit fully inside *some* tile only when its box is no wider than
`tile - step`. Anything bigger can be sliced by every tile that touches it, and
the tile that caught only one end contributes a small partial box that looks
like a second worm. Raising `overlap` raises that guaranteed size:

    overlap 0.20 -> step 541x486 -> whole-object guarantee 135 x 122 px (56 tiles)
    overlap 0.30 -> step 473x426 -> whole-object guarantee 203 x 182 px (63 tiles)
    overlap 0.35 -> step 439x395 -> whole-object guarantee 237 x 213 px (72 tiles)

Per-class confidence
--------------------
`class_conf` maps a class name to its own confidence floor, applied to every raw
detection BEFORE any NMS, so a low-confidence label can never win a merge
against the correct one. The model itself must run at or below the smallest
threshold or those detections never reach us; `conf` is lowered automatically to
guarantee that. Keys are matched case- and whitespace-insensitively, and a
"_default" key covers any class not named explicitly, so a retrained model that
adds a class still runs.

Class exclusion
---------------
`exclude_classes` drops a class from the run entirely, before any merge. The
motivating case is eggs: a plate is almost never a question about worms AND eggs
at the same time, and egg detections are pure clutter on a staging plate.
Dropping them pre-NMS (rather than filtering the output) matters — an egg box
that is never created also cannot suppress a real L1 it overlaps.

A class dropped this way is NOT the same as a class that scored zero. Callers
must report it as "not counted", never as 0; infer_stage.py's counts file and
the meta line both do.

Per-class size gate
-------------------
`class_size_px` rejects a detection whose box is the wrong physical size for the
class it claims to be, measured as `sqrt(w * h)` in full-frame pixels. This
exists because staging is a size readout: the stages are ordered
egg < L1 < L2 < L3 < L4 < young adult < adult, so a 40 px blob labelled "adult"
is not a low-confidence adult, it is not a worm at all — which is why debris can
score high on it and a confidence threshold cannot help. Applied to raw
detections BEFORE any NMS, so an implausible label cannot win a merge.

Detections flagged `truncated` (see below) are EXEMPT: a worm cut by a tile seam
is legitimately smaller than its class, and gating it would delete a real worm
whose only detection came from a tile that clipped it. Seam handling deals with
those separately.

There is deliberately no default for this — the plausible pixel size of a stage
depends on the magnification, so it must be measured. `dev/tools/
stage_conf_report.py --suggest` writes a ready-to-paste block from real images.

Seam cleanups
-------------
Three optional passes, all OFF by default so the bare call reproduces the
original per-class-only behaviour exactly:

  seam_margin (int px) + seam_cover_frac (float): the targeted duplicate fix.
    A detection whose tile-local box comes within `seam_margin` px of an
    INTERIOR tile border is flagged truncated - it is a candidate worm fragment,
    not a worm. Unlike `edge_margin` below, it is NOT dropped on the spot.
    After the merge, a truncated box is dropped only if at least
    `seam_cover_frac` of its area lies inside a higher-scoring box of ANY class,
    i.e. only when a better box already covers that worm. A fragment with
    nothing covering it survives, so a worm is never lost outright. Interior
    seams only, never the true frame border, so real worms at the image edge are
    untouched.

  edge_margin (int px): the blunt version - drop an interior-seam detection
    unconditionally. Kept for callers that already depend on it; prefer
    seam_margin + seam_cover_frac, which cannot delete a worm. 0 = off.

  class_agnostic_iou (float): after per-class NMS, run one more NMS across ALL
    surviving boxes regardless of class. Collapses two labels on ONE object —
    the same worm called L3 in one tile and L4 in another, at nearly the same
    box — down to the single higher-confidence call. Per-class NMS structurally
    cannot do this: it never compares boxes of different classes.

    This is IoU-based, so it only fires on near-coincident boxes. That is the
    point: at a high threshold two boxes must be essentially the same rectangle
    to be merged, which one object can produce and two neighbouring worms
    realistically cannot. Lower it if same-worm pairs persist; raise it if two
    genuinely adjacent worms are being collapsed into one. It is NOT a
    substitute for the seam_cover_frac pass — a small box nested inside a large
    one has low IoU by construction. None or >=1.0 = off.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image


# ---------- tiling ----------

def tile_origins(total, tile, overlap):
    step = max(1, int(round(tile * (1 - overlap))))
    xs = list(range(0, max(1, total - tile + 1), step)) or [0]
    last = max(0, total - tile)
    if xs[-1] != last:
        xs.append(last)
    return xs


# ---------- per-class confidence ----------

def _norm_key(s) -> str:
    return " ".join(str(s).strip().lower().split())


def resolve_class_conf(class_conf, names, fallback=None) -> dict:
    """Expand a per-class threshold mapping to {class_name: float} for every
    class in `names`.

    `class_conf` is matched case- and whitespace-insensitively and may carry a
    "_default" key for classes it does not name. `names` is the model's
    {index: name} dict (or any iterable of names). Classes with no entry and no
    "_default" fall back to `fallback`.

    Returned keys are the model's own spelling of each class, so the result can
    be looked up directly with the class name tiled_infer() attaches to a box.
    """
    src = {_norm_key(k): float(v) for k, v in (class_conf or {}).items()}
    default = src.get("_default", fallback)
    labels = names.values() if isinstance(names, dict) else names
    out = {}
    for name in labels:
        val = src.get(_norm_key(name), default)
        if val is not None:
            out[str(name)] = float(val)
    return out


# ---------- NMS ----------

def iou_batch(box, boxes):
    x1 = np.maximum(box[0], boxes[:, 0]); y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2]); y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    a = (box[2] - box[0]) * (box[3] - box[1])
    b = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return inter / (a + b - inter + 1e-9)


def box_size_px(x1, y1, x2, y2) -> float:
    """Size proxy for a detection: sqrt(w * h), in pixels.

    Geometric mean rather than the longer side, because a coiled worm and the
    same worm stretched out have very different max-sides but similar box areas.
    A stage's plausible range is therefore much tighter on this than on either
    dimension alone.
    """
    return math.sqrt(max(0.0, x2 - x1) * max(0.0, y2 - y1))


def size_ok(size: float, bounds) -> bool:
    """True when `size` sits inside `bounds`, a (min, max) pair with either
    element allowed to be None (meaning unbounded on that side)."""
    if not bounds:
        return True
    lo, hi = (list(bounds) + [None, None])[:2]
    if lo is not None and size < float(lo):
        return False
    if hi is not None and size > float(hi):
        return False
    return True


def resolve_class_size(class_size_px, names) -> dict:
    """Expand a per-class size mapping to {class_name: (min, max)}.

    Matched case- and whitespace-insensitively like resolve_class_conf. Classes
    with no entry are absent from the result, i.e. ungated — a new class from a
    retrain is never silently size-filtered against numbers measured on a
    different model.
    """
    src = {_norm_key(k): v for k, v in (class_size_px or {}).items()
           if not str(k).startswith("_")}
    labels = names.values() if isinstance(names, dict) else names
    out = {}
    for name in labels:
        bounds = src.get(_norm_key(name))
        if bounds:
            lo, hi = (list(bounds) + [None, None])[:2]
            out[str(name)] = (None if lo is None else float(lo),
                              None if hi is None else float(hi))
    return out


def covered_fraction(box, other) -> float:
    """Fraction of `box`'s own area that lies inside `other`.

    This is intersection-over-SELF, not IoU. IoU is the wrong test for a box
    nested inside a bigger one: a fragment a third the area of the good box has
    IoU <= 0.33 even when it is entirely contained, so it sails through an
    IoU 0.45 NMS. Intersection-over-self reads 1.0 for that same pair.
    """
    x1 = max(box[0], other[0]); y1 = max(box[1], other[1])
    x2 = min(box[2], other[2]); y2 = min(box[3], other[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = max(1e-9, (box[2] - box[0]) * (box[3] - box[1]))
    return inter / area


def nms(boxes, scores, iou_thr):
    """Standard NMS. Ignores class by design - callers group by class first if
    they want per-class behaviour. Returns kept indices."""
    if len(boxes) == 0:
        return []
    idxs = scores.argsort()[::-1]
    keep = []
    while len(idxs):
        i = idxs[0]; keep.append(i)
        if len(idxs) == 1:
            break
        rest = idxs[1:]
        idxs = rest[iou_batch(boxes[i], boxes[rest]) < iou_thr]
    return keep


def suppress_seam_fragments(dets, cover_frac):
    """Drop truncated boxes that a better box already covers.

    `dets` is a list of [x1, y1, x2, y2, score, class_name, truncated]. Walking
    in descending score and only ever testing a candidate against boxes ALREADY
    kept makes this order-deterministic and mutually safe: the highest-scoring
    box of any overlapping group is always kept, so two truncated halves of one
    worm can never annihilate each other, and a truncated box with nothing
    covering it survives as the only evidence of that worm.

    Comparison is class-agnostic on purpose - the case this exists for is a
    stub labelled L2 sitting inside the correct L3 box, which per-class NMS
    never even compares.
    """
    kept = []
    for det in sorted(dets, key=lambda d: -d[4]):
        if det[6] and any(covered_fraction(det[:4], k[:4]) >= cover_frac
                          for k in kept):
            continue
        kept.append(det)
    return kept


# ---------- tiled inference ----------

def tiled_infer(frame, model, names, *, tile_w=676, tile_h=608, overlap=0.2,
                conf=0.15, iou=0.45, edge_margin=0,
                class_agnostic_iou=None, imgsz=640,
                class_conf=None, seam_margin=0, seam_cover_frac=None,
                class_size_px=None, exclude_classes=None):
    """Tile `frame`, run `model` per tile, return merged full-frame detections.

    frame: PIL.Image or RGB np.ndarray of shape (H, W, 3).
    model: loaded ultralytics YOLO.
    names: dict {class_index: class_name}.

    Returns a list of [x1, y1, x2, y2, score, class_name] in full-frame coords.

    With the default class_conf=None, class_size_px=None, exclude_classes=None,
    seam_margin=0, edge_margin=0 and class_agnostic_iou=None the result is
    identical to the original per-class tile-merge in tiled_assist.py.
    """
    if isinstance(frame, Image.Image):
        arr = np.array(frame.convert("RGB"))
    else:
        arr = np.asarray(frame)
    H, W = arr.shape[:2]

    # Per-class floors are applied to raw detections below; the model has to run
    # at or under the smallest of them or those boxes never reach us at all.
    thresholds = resolve_class_conf(class_conf, names) if class_conf else {}
    if thresholds:
        conf = min(conf, min(thresholds.values()))
    size_bounds = resolve_class_size(class_size_px, names) if class_size_px else {}
    dropped = {_norm_key(c) for c in (exclude_classes or [])}

    raw = []
    for oy in tile_origins(H, tile_h, overlap):
        for ox in tile_origins(W, tile_w, overlap):
            tile = arr[oy:oy + tile_h, ox:ox + tile_w]
            th, tw = tile.shape[:2]  # ACTUAL dims - edge tiles are smaller
            # ultralytics expects BGR for numpy input; flip channels
            tile_bgr = np.ascontiguousarray(tile[:, :, ::-1])
            res = model.predict(tile_bgr, imgsz=imgsz, conf=conf, verbose=False)[0]
            if res.boxes is None or len(res.boxes) == 0:
                continue
            xyxy = res.boxes.xyxy.cpu().numpy()
            sconf = res.boxes.conf.cpu().numpy()
            cls = res.boxes.cls.cpu().numpy().astype(int)
            for (bx1, by1, bx2, by2), sc, ci in zip(xyxy, sconf, cls):
                stage = names.get(int(ci), str(ci))
                # Excluded classes never enter the pool at all, so they cannot
                # win — or lose — a merge against a class we do want.
                if dropped and _norm_key(stage) in dropped:
                    continue
                # Per-class floor, before any merge: a label that did not clear
                # its own threshold must not be allowed to win NMS against the
                # label that did.
                if thresholds and float(sc) < thresholds.get(stage, 0.0):
                    continue
                # Interior-seam tests, in TILE-LOCAL coords, BEFORE shifting to
                # full-frame (we need to know which tile it came from and
                # whether the border it touches is a seam or the real frame
                # edge). `ox > 0` means there is a tile to the left, so the left
                # border of this tile is a seam; `ox + tw < W` likewise on the
                # right.
                if edge_margin > 0:
                    if ((ox > 0 and bx1 <= edge_margin) or
                        (ox + tw < W and bx2 >= tw - edge_margin) or
                        (oy > 0 and by1 <= edge_margin) or
                        (oy + th < H and by2 >= th - edge_margin)):
                        continue
                truncated = False
                if seam_margin > 0:
                    truncated = bool(
                        (ox > 0 and bx1 <= seam_margin) or
                        (ox + tw < W and bx2 >= tw - seam_margin) or
                        (oy > 0 and by1 <= seam_margin) or
                        (oy + th < H and by2 >= th - seam_margin)
                    )
                # Size plausibility, also before the merge. Skipped for
                # truncated boxes: a seam-clipped worm is legitimately undersized
                # for its stage, and dropping it here could delete the only
                # detection of a real worm.
                if size_bounds and not truncated:
                    if not size_ok(box_size_px(bx1, by1, bx2, by2),
                                   size_bounds.get(stage)):
                        continue
                raw.append([ox + bx1, oy + by1, ox + bx2, oy + by2,
                            float(sc), stage, truncated])

    if not raw:
        return []

    boxes = np.array([r[:4] for r in raw], dtype=float)
    scores = np.array([r[4] for r in raw], dtype=float)
    classes = np.array([r[5] for r in raw])
    truncs = np.array([r[6] for r in raw], dtype=bool)

    # per-class NMS (exactly as before)
    dets = []
    for c in np.unique(classes):
        m = classes == c
        keep = nms(boxes[m], scores[m], iou)
        for b, s, t in zip(boxes[m][keep], scores[m][keep], truncs[m][keep]):
            dets.append([b[0], b[1], b[2], b[3], float(s), str(c), bool(t)])

    # optional final cross-class pass to collapse seam duplicates
    if class_agnostic_iou is not None and class_agnostic_iou < 1.0 and dets:
        ca_boxes = np.array([d[:4] for d in dets], dtype=float)
        ca_scores = np.array([d[4] for d in dets], dtype=float)
        keep = nms(ca_boxes, ca_scores, class_agnostic_iou)
        dets = [dets[i] for i in keep]

    # targeted duplicate fix: drop seam fragments that a better box covers
    if seam_margin > 0 and seam_cover_frac is not None and dets:
        dets = suppress_seam_fragments(dets, float(seam_cover_frac))

    # the truncated flag is internal bookkeeping; callers get the 6-tuple
    return [d[:6] for d in dets]
