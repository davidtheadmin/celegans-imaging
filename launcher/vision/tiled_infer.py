#!/usr/bin/env python3
"""
tiled_infer.py - shared tiled inference for the WormScan staging pipeline.

Single implementation of: tile a full 4056x3040 frame at 676x608, run a YOLO
staging model on each tile, shift boxes back to full-frame coords, merge.

Everything that needs tiled detection (assist labeling, conf histograms,
per-class thresholds, the launcher snapshot button) should import tiled_infer()
from here so the tiling params and NMS logic live in exactly one place. If any
caller drifts from 676x608 resize-to-640, staging accuracy degrades invisibly,
so don't.

Two optional seam cleanups, both OFF by default so the default call reproduces
the original per-class-only behaviour exactly:

  edge_margin (int px): drop a detection that touches an INTERIOR tile seam
    within this many pixels of the border. Targets worms cut by a seam - the
    fragment from the tile that only caught one end. Fires only on interior
    seams, never the true frame border, so real worms at the image edge survive.
    Pair with a wider `overlap` so the whole worm is fully contained in some
    other tile, which then supplies the correct box. 0 = off.

  class_agnostic_iou (float): after per-class NMS, run one more NMS across ALL
    surviving boxes regardless of class. Collapses seam duplicates that got
    different stage labels in different tiles (an L2 box and an L3 box on the
    same worm) down to the single higher-confidence call. None or >=1.0 = off.
"""

from __future__ import annotations

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


# ---------- NMS ----------

def iou_batch(box, boxes):
    x1 = np.maximum(box[0], boxes[:, 0]); y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2]); y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    a = (box[2] - box[0]) * (box[3] - box[1])
    b = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return inter / (a + b - inter + 1e-9)


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


# ---------- tiled inference ----------

def tiled_infer(frame, model, names, *, tile_w=676, tile_h=608, overlap=0.2,
                conf=0.15, iou=0.45, edge_margin=0,
                class_agnostic_iou=None, imgsz=640):
    """Tile `frame`, run `model` per tile, return merged full-frame detections.

    frame: PIL.Image or RGB np.ndarray of shape (H, W, 3).
    model: loaded ultralytics YOLO.
    names: dict {class_index: class_name}.

    Returns a list of [x1, y1, x2, y2, score, class_name] in full-frame coords.

    With edge_margin=0 and class_agnostic_iou=None (the defaults) the result is
    identical to the original per-class tile-merge in tiled_assist.py.
    """
    if isinstance(frame, Image.Image):
        arr = np.array(frame.convert("RGB"))
    else:
        arr = np.asarray(frame)
    H, W = arr.shape[:2]

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
                # interior-seam truncation filter, in TILE-LOCAL coords,
                # BEFORE shifting to full-frame (need to know which tile it came
                # from and whether the border it touches is a seam or the frame)
                if edge_margin > 0:
                    if ((ox > 0 and bx1 <= edge_margin) or
                        (ox + tw < W and bx2 >= tw - edge_margin) or
                        (oy > 0 and by1 <= edge_margin) or
                        (oy + th < H and by2 >= th - edge_margin)):
                        continue
                raw.append([ox + bx1, oy + by1, ox + bx2, oy + by2,
                            float(sc), names.get(int(ci), str(ci))])

    if not raw:
        return []

    boxes = np.array([r[:4] for r in raw], dtype=float)
    scores = np.array([r[4] for r in raw], dtype=float)
    classes = np.array([r[5] for r in raw])

    # per-class NMS (exactly as before)
    dets = []
    for c in np.unique(classes):
        m = classes == c
        keep = nms(boxes[m], scores[m], iou)
        for b, s in zip(boxes[m][keep], scores[m][keep]):
            dets.append([b[0], b[1], b[2], b[3], float(s), str(c)])

    # optional final cross-class pass to collapse seam duplicates
    if class_agnostic_iou is not None and class_agnostic_iou < 1.0 and dets:
        ca_boxes = np.array([d[:4] for d in dets], dtype=float)
        ca_scores = np.array([d[4] for d in dets], dtype=float)
        keep = nms(ca_boxes, ca_scores, class_agnostic_iou)
        dets = [dets[i] for i in keep]

    return dets
