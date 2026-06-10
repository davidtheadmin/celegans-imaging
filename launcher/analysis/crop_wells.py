"""Auto-detect the circular well in a plate still and crop to it.

Designed for WormScan clonogenic/colony stills where one well fills most of
the frame and neighbouring wells intrude at the edges. The well floor is found
by segmenting the bright growth surface (the well floor is a bright disk on a
darker surround), which is robust to the plate sitting slightly off-centre under
the lens. When the plate is off-centre the cylindrical wall is visible and the
top opening sits higher in frame than the floor; edge-based circle finders tend
to lock onto the top rim, so floor segmentation is preferred and an edge-based
Hough transform is used only as a fallback. The region outside the floor is then
masked so downstream counters (Cellpose, OpenCFU, ColonyArea) don't pick up
colonies from adjacent wells.

Use as a library:
    from crop_wells import detect_well, crop_to_well
    circle = detect_well(gray)              # (cx, cy, r) in full-res pixels
    cropped, mask = crop_to_well(img, circle, fill="median")

Or as a CLI:
    python crop_wells.py INPUT_DIR -o OUTPUT_DIR --fill median --qc

Dependencies: opencv-python(-headless), numpy, tifffile (+imagecodecs for LZW TIFF).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

try:
    import tifffile
except ImportError:  # tifffile optional if you only feed jpg/png
    tifffile = None


# ----------------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------------
def detect_well(gray: np.ndarray, radius=None, detect_scale: float = 0.25):
    """Find the well floor circle. Returns (cx, cy, r) full-res px, or None.

    Primary method: from a rough centre, find the floor edge (bright-floor ->
    dark-ring drop) along many rays, then fit a circle to those edge points with
    iterative outlier rejection. Using the edge geometry directly avoids the
    centre being pulled by the bright rim halo. If `radius` is given, the fitted
    centre is kept but the radius is overridden. Falls back to template-match /
    Hough if the fit fails.
    """
    fit = _detect_edgefit(gray, detect_scale)
    if fit is not None:
        cx, cy, r = fit
        return (cx, cy, float(radius)) if radius is not None else (cx, cy, r)

    # fallbacks
    anchor = _floor_anchor(gray, detect_scale)
    if anchor is not None:
        r = radius if radius is not None else (
            _floor_radius(gray, anchor[0], anchor[1], anchor[2], detect_scale) or anchor[2])
        cxy = _best_center(gray, r, detect_scale)
        if cxy is not None:
            return (cxy[0], cxy[1], float(r))
        return (anchor[0], anchor[1], float(r))
    cands = _hough_candidates(gray, detect_scale)
    if cands:
        H, W = gray.shape[:2]
        return min(cands, key=lambda c: (c[0] - W / 2) ** 2 + (c[1] - H / 2) ** 2)
    return None


def _fit_circle(pts):
    """Algebraic least-squares circle fit. pts: Nx2. Returns (cx, cy, r)."""
    x, y = pts[:, 0], pts[:, 1]
    A = np.c_[2 * x, 2 * y, np.ones(len(x))]
    b = x * x + y * y
    cx, cy, c = np.linalg.lstsq(A, b, rcond=None)[0]
    return cx, cy, np.sqrt(c + cx * cx + cy * cy)


def _detect_edgefit(gray, detect_scale=0.25, min_drop=3.0):
    """Find floor-edge points along rays and fit a circle with outlier rejection."""
    anchor = _floor_anchor(gray, detect_scale)
    if anchor is None:
        return None
    cx, cy, req = (v * detect_scale for v in anchor)
    g = cv2.GaussianBlur(cv2.resize(gray, (int(gray.shape[1] * detect_scale),
                                            int(gray.shape[0] * detect_scale))),
                         (0, 0), 2).astype(np.float32)
    h, w = g.shape
    rs = np.arange(0.55 * req, 1.25 * req, 1.0)
    if len(rs) < 4:
        return None
    pts = []
    for ang in np.linspace(0, 2 * np.pi, 360, endpoint=False):
        xs = cx + rs * np.cos(ang)
        ys = cy + rs * np.sin(ang)
        m = (xs >= 0) & (xs < w - 1) & (ys >= 0) & (ys < h - 1)
        if m.sum() < 10:
            continue
        prof = g[ys[m].astype(int), xs[m].astype(int)]
        d = np.diff(prof)
        i = int(np.argmin(d))
        if -d[i] < min_drop:          # require a real bright->dark step
            continue
        pts.append((xs[m][i], ys[m][i]))
    if len(pts) < 12:
        return None
    pts = np.array(pts, dtype=np.float64)
    for _ in range(5):                # iteratively drop points far from the fit
        cxf, cyf, rf = _fit_circle(pts)
        dist = np.abs(np.hypot(pts[:, 0] - cxf, pts[:, 1] - cyf) - rf)
        keep = dist < np.percentile(dist, 80)
        if keep.sum() < 12:
            break
        pts = pts[keep]
    cxf, cyf, rf = _fit_circle(pts)
    return cxf / detect_scale, cyf / detect_scale, rf / detect_scale


def _floor_mask(gray, detect_scale=0.25):
    H, W = gray.shape[:2]
    g = cv2.GaussianBlur(cv2.resize(gray, (int(W * detect_scale), int(H * detect_scale))),
                         (0, 0), 3)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, k)
    return th


def _best_center(gray, radius, detect_scale=0.25):
    """Position of best overlap between a radius-`radius` disk and the floor mask."""
    th = (_floor_mask(gray, detect_scale) > 0).astype(np.float32)
    rs = max(1, int(radius * detect_scale))
    yy, xx = np.ogrid[-rs:rs + 1, -rs:rs + 1]
    disk = ((xx * xx + yy * yy) <= rs * rs).astype(np.float32)
    if disk.sum() == 0:
        return None
    disk /= disk.sum()
    score = cv2.filter2D(th, -1, disk, borderType=cv2.BORDER_CONSTANT)
    _, _, _, maxloc = cv2.minMaxLoc(score)
    return (maxloc[0] / detect_scale, maxloc[1] / detect_scale)


def _floor_radius(gray, cx, cy, r_eq, detect_scale=0.25,
                  r_lo_frac=0.45, r_hi_frac=1.15, n_rays=180):
    """Median radius of the floor-edge drop, measured from (cx, cy)."""
    H, W = gray.shape[:2]
    g = cv2.GaussianBlur(cv2.resize(gray, (int(W * detect_scale), int(H * detect_scale))),
                         (0, 0), 2).astype(np.float32)
    h, w = g.shape
    cxs, cys, r0 = cx * detect_scale, cy * detect_scale, r_eq * detect_scale
    rs = np.arange(r_lo_frac * r0, r_hi_frac * r0, 1.0)
    if len(rs) < 4:
        return None
    radii = []
    for ang in np.linspace(0, 2 * np.pi, n_rays, endpoint=False):
        xs = cxs + rs * np.cos(ang)
        ys = cys + rs * np.sin(ang)
        m = (xs >= 0) & (xs < w - 1) & (ys >= 0) & (ys < h - 1)
        if m.sum() < 10:
            continue
        prof = g[ys[m].astype(int), xs[m].astype(int)]
        i = int(np.argmin(np.diff(prof)))   # bright floor -> dark edge ring
        radii.append(rs[m][i])
    if not radii:
        return None
    return float(np.median(radii)) / detect_scale


def _floor_anchor(gray, detect_scale=0.25, min_area_frac=0.10):
    """Largest bright floor blob nearest the frame centre; (cx, cy, r_eq) px."""
    th = _floor_mask(gray, detect_scale)
    h, w = th.shape
    n, lab, stats, cent = cv2.connectedComponentsWithStats(th)
    if n < 2:
        return None
    min_area = min_area_frac * h * w
    cx0, cy0 = w / 2, h / 2
    best, best_d = None, None
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        d = (cent[i, 0] - cx0) ** 2 + (cent[i, 1] - cy0) ** 2
        if best is None or d < best_d:
            best, best_d = i, d
    if best is None:
        return None
    r_eq = np.sqrt(stats[best, cv2.CC_STAT_AREA] / np.pi)
    return (cent[best, 0] / detect_scale, cent[best, 1] / detect_scale,
            r_eq / detect_scale)


def _hough_candidates(gray, detect_scale=0.25, r_frac_min=0.33, r_frac_max=0.50,
                      param1=80, param2=32):
    """Edge-based fallback candidates as (cx, cy, r) in full-res px."""
    H, W = gray.shape[:2]
    g = cv2.resize(gray, (int(W * detect_scale), int(H * detect_scale)))
    g = cv2.medianBlur(g, 5)
    h, w = g.shape
    circles = cv2.HoughCircles(
        g, cv2.HOUGH_GRADIENT, dp=1, minDist=60, param1=param1, param2=param2,
        minRadius=int(min(h, w) * r_frac_min), maxRadius=int(min(h, w) * r_frac_max),
    )
    if circles is None:
        return []
    return [(cx / detect_scale, cy / detect_scale, r / detect_scale)
            for cx, cy, r in circles[0]]


# ----------------------------------------------------------------------------
# Cropping
# ----------------------------------------------------------------------------
def crop_to_well(
    img: np.ndarray,
    circle,
    shrink: float = 0.96,
    pad: int = 20,
    fill: str = "median",
):
    """Mask everything outside the well and crop tight to the circle.

    img    : H x W x 3 BGR (or H x W grayscale) full-resolution image.
    circle : (cx, cy, r) from detect_well, in full-res pixels.
    shrink : pull the crop radius inside the detected rim (0.96 = 4% in) so the
             plastic wall / meniscus reflection is excluded.
    fill   : how to paint outside the circle:
               "median"      -> median colour sampled inside the well (no edges,
                                best for segmentation pipelines)
               "black"/"white"
               "transparent" -> returns BGRA with alpha=0 outside (clean circle)
    Returns (cropped_image, mask) where mask is 255 inside the well.
    """
    H, W = img.shape[:2]
    cx, cy, r = (int(round(v)) for v in circle)
    r_in = int(r * shrink)

    mask = np.zeros((H, W), np.uint8)
    cv2.circle(mask, (cx, cy), r_in, 255, -1)

    if fill == "transparent":
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        out = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        out[mask == 0] = (0, 0, 0, 0)
    else:
        out = img.copy()
        if fill == "median":
            fill_val = np.median(img[mask == 255], axis=0).astype(img.dtype)
        elif fill == "black":
            fill_val = 0
        elif fill == "white":
            fill_val = np.iinfo(img.dtype).max if np.issubdtype(img.dtype, np.integer) else 1.0
        else:
            raise ValueError(f"unknown fill mode: {fill}")
        out[mask == 0] = fill_val

    x1, x2 = max(0, cx - r - pad), min(W, cx + r + pad)
    y1, y2 = max(0, cy - r - pad), min(H, cy + r + pad)
    return out[y1:y2, x1:x2], mask


# ----------------------------------------------------------------------------
# I/O helpers (keep TIFF calibration tags intact; no resampling happens)
# ----------------------------------------------------------------------------
def _read(path: Path):
    """Return (bgr_image, tiff_tags_or_None)."""
    if path.suffix.lower() in (".tif", ".tiff") and tifffile is not None:
        with tifffile.TiffFile(path) as t:
            p = t.pages[0]
            arr = p.asarray()
            tags = {
                "resolution": (
                    p.tags["XResolution"].value if "XResolution" in p.tags else None,
                    p.tags["YResolution"].value if "YResolution" in p.tags else None,
                ),
                "resolutionunit": p.tags["ResolutionUnit"].value if "ResolutionUnit" in p.tags else None,
                "description": p.tags["ImageDescription"].value if "ImageDescription" in p.tags else None,
            }
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if arr.ndim == 3 else arr
        return bgr, tags
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    return img, None


def _write(path: Path, img: np.ndarray, tags):
    if path.suffix.lower() in (".tif", ".tiff") and tifffile is not None and img.shape[-1] != 4:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB) if img.ndim == 3 else img
        kw = {}
        if tags:
            xr, yr = tags.get("resolution", (None, None))
            if xr:
                kw["resolution"] = (xr, yr or xr)
            if tags.get("description"):
                kw["description"] = tags["description"]
        tifffile.imwrite(path, rgb, compression="lzw", **kw)
    else:
        # PNG handles BGRA (transparent) and preserves lossless quality
        cv2.imwrite(str(path), img)


# ----------------------------------------------------------------------------
# Batch / CLI
# ----------------------------------------------------------------------------
def process_dir(in_dir, out_dir, fill="median", shrink=0.96, pad=20,
                ext="tif", qc=False, radius=None):
    """Crop every plate still in in_dir to its well.

    radius: None -> fit each well independently (centre + radius from the floor
    edge). A number -> keep the fitted centre but force that radius.
    """
    in_dir, out_dir = Path(in_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_ext = ".png" if fill == "transparent" else f".{ext.lstrip('.')}"

    inputs = sorted(p for p in in_dir.iterdir()
                    if p.suffix.lower() in (".tif", ".tiff", ".jpg", ".jpeg", ".png"))
    for path in inputs:
        img, tags = _read(path)
        if img is None:
            print(f"{path.name}: could not read - skipped")
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        circle = detect_well(gray, radius=radius)
        if circle is None:
            print(f"{path.name}: NO WELL FOUND - skipped")
            continue
        cx, cy, r = circle
        cropped, mask = crop_to_well(img, circle, shrink=shrink, pad=pad, fill=fill)
        stem = path.stem.replace("_still", "")
        out_path = out_dir / f"{stem}_cropped{out_ext}"
        _write(out_path, cropped, tags)
        print(f"{path.name}: center=({cx:.0f},{cy:.0f}) r={r:.0f}px "
              f"-> {out_path.name} {cropped.shape[1]}x{cropped.shape[0]}")
        if qc:
            ov = (cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img).copy()
            cv2.circle(ov, (int(cx), int(cy)), int(r), (0, 0, 255), 6)
            cv2.circle(ov, (int(cx), int(cy)), int(r * shrink), (0, 255, 0), 6)
            qh = 760
            cv2.imwrite(str(out_dir / f"{stem}_qc.png"),
                        cv2.resize(ov, (int(ov.shape[1] * qh / ov.shape[0]), qh)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Auto-crop plate stills to the well circle.")
    ap.add_argument("input_dir")
    ap.add_argument("-o", "--output_dir", default="cropped")
    ap.add_argument("--fill", choices=["median", "black", "white", "transparent"],
                    default="median")
    ap.add_argument("--shrink", type=float, default=0.96,
                    help="crop radius as fraction of detected radius (default 0.96)")
    ap.add_argument("--pad", type=int, default=20)
    ap.add_argument("--ext", default="tif", help="output ext for non-transparent fills")
    ap.add_argument("--radius", type=float, default=None,
                    help="fixed well radius in px; omit to auto-estimate once per batch")
    ap.add_argument("--qc", action="store_true", help="also write detection overlay PNGs")
    args = ap.parse_args()
    process_dir(args.input_dir, args.output_dir, fill=args.fill,
                shrink=args.shrink, pad=args.pad, ext=args.ext, qc=args.qc,
                radius=args.radius)
