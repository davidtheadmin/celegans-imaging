"""Clonogenic colony counter for single-well crystal-violet stills.

Builds on the validated well-cropping module (``crop_wells.py``), which is
treated as a black box: this module imports ``detect_well`` (well circle/mask)
and ``_read`` (TIFF-tag-preserving reader) and never modifies them.

Pipeline per image (experiment/condition/plateNN/<one image>):
  1. read + detect well circle -> circular analysis mask (count only inside)
  2. derive um/px from the detected radius (or TIFF tags) for physical sizes
  3. build a stain map that surfaces faint colonies (green / OD / gray), then
     flatten illumination with a gentle large-kernel white-tophat
  3b. optionally blur that map (--smooth-um) so feathery, non-solid colonies
     read as one object rather than a spray of fragments; detection only,
     intensity is still measured on the unblurred map
  4. threshold inside the mask -- either the automatic (per-plate) threshold
     scaled by the detection-sensitivity dial (--sensitivity), or one absolute
     optical-density level applied to every plate (--threshold fixed), which is
     what makes counts comparable across a dose series -- then split touching
     colonies with a distance-transform + h-maxima marker-controlled watershed
     (NOT plain connected components)
  5. filter by real colony diameter, well-boundary contact, and solidity
  6. confluence fallback: if the stained-area fraction is high, flag the count
     unreliable but still report both count and stained area

Outputs (in the analysis dir): counting_results.xlsx (per_colony / per_plate /
per_condition), counting_summary.csv (= per_condition), overlays/*.png for
manual validation, and log.txt.

CLI:
    python counting.py <experiment_dir> -o <output_dir> [options]

Dependencies: numpy, opencv-python(-headless), scipy, scikit-image, pandas,
openpyxl, tifffile (+imagecodecs for LZW TIFF) -- all already shipped by the
launcher.
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, fields as _dc_fields
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

import cv2
import numpy as np
import pandas as pd
from scipy import ndimage as ndi
from skimage.measure import regionprops
from skimage.morphology import h_maxima
from skimage.segmentation import find_boundaries, watershed

try:  # run as a script from launcher/analysis, or as part of the package
    from crop_wells import detect_well, _read as read_image
except ImportError:  # pragma: no cover - import path shim
    from analysis.crop_wells import detect_well, _read as read_image


_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3
_ANALYSIS_PREFIX = "_counting_analysis"


# Detection sensitivity: a single 0-10 dial the dialog exposes, mapped to a
# multiplier on the automatic (Otsu) threshold. NEUTRAL reproduces the
# pre-slider behaviour exactly, so an untouched install counts as it always did;
# SPAN sets how far each end of the dial can move the threshold.
_SENS_NEUTRAL = 5.0
_SENS_SPAN = 2.5


def threshold_scale(sensitivity: float) -> float:
    """Map the 0-10 sensitivity dial to a multiplier on the auto threshold.

    Geometric around the neutral point so each step is the same *relative* move:
    5 -> 1.00 (unchanged), 7.5 -> 0.63, 10 -> 0.40 (picks up faint/sparse
    colonies), 2.5 -> 1.58, 0 -> 2.50 (only the darkest cores).
    """
    s = min(10.0, max(0.0, float(sensitivity)))
    return float(_SENS_SPAN ** ((_SENS_NEUTRAL - s) / _SENS_NEUTRAL))


# THE WELL CIRCLE COMES FROM THE CAPTURE UI, NOT FROM THE IMAGE.
#
# The colony screen in the imaging UI draws a crosshair and an aim circle, and
# the operator frames the well to it, so where the well sits in the frame is
# already known before analysis starts: centred, with a radius that is a fixed
# fraction of the frame's short side.
#
# Detecting that circle instead means re-deriving something we already know
# from the least reliable part of a stained image — its rim — and when the fit
# slips the error lands in two numbers at once: which colonies are inside the
# mask, and the micrometres per pixel. On the first dose series analysed this
# way the fitted radius ranged 952-1255 px across 36 wells of the same plate
# type, a 30% swing in scale; the oversized fits pulled the plastic rim into
# the mask and counted its texture as colonies, and the undersized ones cut a
# ring of real colonies out of the well. A fixed circle cannot slip, is
# identical for every plate in a dose series — the same argument as the fixed
# absolute threshold — and puts the responsibility on the one thing that can
# actually see the well: the person at the microscope, aiming.
#
# THE FRACTION IS MEASURED, NOT ASSUMED. 0.39 is where the well floor edge sits
# on full-resolution stills from this rig (checked against the floor/rim
# boundary on stained wells; 0.35 lands visibly inside the floor and 0.41 on
# the outer rim). GUIDED_CIRCLE_FRAC in capture/app/static/app.js draws the
# on-screen circle at the same fraction of the preview image, so what the
# operator aims with and what the analysis masks with are the same circle.
# `mask_shrink` then holds the analysis mask a little inside that edge. It is
# set to leave real slack rather than to hug the rim: losing a few colonies in
# the outermost ring costs a small, EVEN fraction of every plate in the run,
# which a survival ratio divides straight back out, while a mask that catches
# the rim on one plate invents colonies out of moulding texture on that plate
# alone. The two errors are not the same size, so the margin is not a
# compromise between them.
#
# `well_mode="auto"` restores per-image detection for stills that were not
# framed to the aim circle.
_AIM_CIRCLE_FRAC = 0.39        # keep in sync with GUIDED_CIRCLE_FRAC in app.js


def aim_circle(shape, frac: float = _AIM_CIRCLE_FRAC) -> tuple[float, float, float]:
    """The capture UI's aim circle in image pixels: centred, radius frac x the
    short side. Resolution-independent, so a future camera or a downscaled copy
    lands in the same place."""
    h, w = shape[:2]
    return w / 2.0, h / 2.0, frac * min(w, h)


def well_circle(gray, opts, tag: str, write_log):
    """(cx, cy, r, source) for one image, or None if there is no usable well.

    "aim" never fails, which is the point: an unreadable rim used to cost the
    whole plate a row in the workbook. "auto" keeps the old behaviour, skip and
    all.
    """
    mode = str(getattr(opts, "well_mode", "aim") or "aim").lower()
    frac = float(getattr(opts, "aim_circle_frac", _AIM_CIRCLE_FRAC) or
                 _AIM_CIRCLE_FRAC)
    if mode == "aim":
        h, w = gray.shape[:2]
        cx, cy, r = aim_circle(gray.shape, frac)
        if abs((w / h) - (4 / 3)) > 0.02:
            write_log(f"{tag}: WARNING image is {w}x{h}, not the 4:3 the aim "
                      f"circle was calibrated on — check overlays/, or use "
                      f"--well-mode auto for this folder")
        return cx, cy, r, f"aim-circle frac={frac:g}"
    circle = detect_well(gray)
    if circle is None:
        write_log(f"{tag}: NO WELL FOUND - skipped")
        return None
    cx, cy, r = circle
    return cx, cy, r, "detected"


@dataclass
class CountingOptions:
    """Per-run knobs consumed by process_image / find_images. Field names and
    defaults mirror the CLI exactly, so process_image accepts either an argparse
    Namespace (CLI) or a CountingOptions (agent) interchangeably."""
    split_sensitivity: float = 3.0
    min_colony_um: float = 200.0
    sensitivity: float = _SENS_NEUTRAL
    smooth_um: float = 0.0
    od_threshold: float = 0.0
    well_diameter_mm: float = 34.8
    stain_channel: str = "od"
    threshold: str = "otsu"
    background_radius_um: float = 3000.0
    min_solidity: float = 0.5
    confluence_frac: float = 0.55
    mask_shrink: float = 0.915
    well_mode: str = "aim"
    aim_circle_frac: float = _AIM_CIRCLE_FRAC
    max_depth: int = _MAX_DEPTH


# ----------------------------------------------------------------------------
# Discovery  (ported from ffmpeg_utils.find_videos / crawling._resolve_video_path
# so it skips _/. dirs and maps folder depth to condition/plate identically;
# crop_wells.py has no find_images, despite the brief, so the semantics are
# reproduced here rather than inventing new ones.)
# ----------------------------------------------------------------------------
def find_images(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Recursively find image files up to ``max_depth`` levels deep, skipping
    dirs whose name starts with '_' (pipeline output) or '.' (hidden caches)."""
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


def resolve_image_path(image: Path, root: Path) -> tuple[str, str]:
    """Return (condition, plate) for an image relative to ``root``.

    Depth 0  root/img                   -> condition="default", plate=img.stem
    Depth 1  root/plate/img             -> condition="default", plate=parent.name
    Depth 2+ root/condition/plate/img   -> condition=grandparent.name, plate=parent.name
    """
    try:
        rel = image.relative_to(root)
    except ValueError:
        return "default", image.stem
    depth = len(rel.parts) - 1
    if depth == 0:
        return "default", image.stem
    if depth == 1:
        return "default", image.parent.name
    return image.parent.parent.name, image.parent.name


# ----------------------------------------------------------------------------
# Scale
# ----------------------------------------------------------------------------
def _rational(v) -> float | None:
    """tifffile rationals arrive as (num, den); also accept a plain number."""
    if v is None:
        return None
    if isinstance(v, (tuple, list)) and len(v) == 2:
        num, den = v
        return float(num) / float(den) if den else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_scale(well_radius_px: float, tags: dict | None,
                  well_diameter_mm: float) -> tuple[float | None, str]:
    """um/px from the detected well radius, else TIFF tags, else None.

    Returns (um_per_px_or_None, human_readable_source).
    """
    if well_diameter_mm and well_diameter_mm > 0:
        upp = (well_diameter_mm * 1000.0) / (2.0 * well_radius_px)
        return upp, f"well-diameter={well_diameter_mm}mm r={well_radius_px:.0f}px"

    if tags:
        res = tags.get("resolution") or (None, None)
        xres = _rational(res[0])
        unit = tags.get("resolutionunit")
        desc = tags.get("description") or ""
        if xres and xres > 0:
            if unit == 2:  # inch
                return 25400.0 / xres, f"TIFF XRes inch ({xres:.1f}px/in)"
            if unit == 3:  # centimeter
                return 10000.0 / xres, f"TIFF XRes cm ({xres:.1f}px/cm)"
            # ResolutionUnit=NONE (1) with ImageJ unit=um: XRes is px-per-um.
            if unit in (1, None) and re.search(r"unit\s*=\s*(um|micron|micrometer|µm)",
                                               desc, re.I):
                return 1.0 / xres, f"TIFF XRes ImageJ um ({xres:.4f}px/um)"
    return None, "none (px only)"


# ----------------------------------------------------------------------------
# Stain map + illumination flattening
# ----------------------------------------------------------------------------
def build_stain_map(bgr: np.ndarray, mask: np.ndarray, channel: str) -> np.ndarray:
    """Float32 map, high where colonies are. Colonies absorb green / are darker.

    green : inverted green channel
    od    : optical density -log10((G+1)/(I0+1)), I0 = bright-floor reference
    gray  : inverted grayscale
    """
    if bgr.ndim == 3:
        green = bgr[:, :, 1].astype(np.float32)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    else:
        green = gray = bgr.astype(np.float32)

    if channel == "green":
        return float(green.max()) - green
    if channel == "gray":
        return float(gray.max()) - gray
    if channel == "od":
        floor = green[mask > 0]
        i0 = float(np.percentile(floor, 95)) if floor.size else float(green.max())
        od = -np.log10((green + 1.0) / (i0 + 1.0))
        return np.clip(od, 0.0, None).astype(np.float32)
    raise ValueError(f"unknown stain channel: {channel}")


def flatten(stain: np.ndarray, bg_radius_px: float, scale: float = 0.25) -> np.ndarray:
    """Gentle white-tophat: subtract a large-kernel morphological opening.

    The opening (the background estimate) is computed on a downscaled copy for
    speed, then upsampled. The kernel must be larger than any real colony so the
    opening passes whole colonies through to the background and the tophat does
    NOT hollow them out -- hence the deliberately large default bg radius.
    """
    h, w = stain.shape
    small = cv2.resize(stain, (max(1, int(w * scale)), max(1, int(h * scale))),
                       interpolation=cv2.INTER_AREA)
    rk = max(1, int(round(bg_radius_px * scale)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rk + 1, 2 * rk + 1))
    bg_small = cv2.morphologyEx(small, cv2.MORPH_OPEN, k)
    bg = cv2.resize(bg_small, (w, h), interpolation=cv2.INTER_LINEAR)
    out = stain - bg
    np.clip(out, 0.0, None, out=out)
    return out


# ----------------------------------------------------------------------------
# Segmentation
# ----------------------------------------------------------------------------
def smooth_stain(stain: np.ndarray, smooth_um: float,
                 um_per_px: float | None) -> np.ndarray:
    """Gaussian-blur the stain map at a colony-relevant scale, or pass through.

    Colonies that grow as loose, feathery clusters (adherent mammalian lines
    stained with crystal violet, say) are not solid discs: at full resolution a
    single colony is a spray of stained specks with pale gaps. Thresholding that
    texture directly splinters one colony into dozens of fragments and loses its
    faint halo entirely. Blurring first at roughly one colony-feature width
    turns the spray back into one blob, so the threshold and the watershed both
    see colonies instead of texture. 0 disables it (the historical behaviour).

    Only the *detection* map is blurred: intensity is still measured on the
    unsmoothed map, so mean_stain / integrated_stain keep their meaning.
    """
    if not smooth_um or smooth_um <= 0:
        return stain
    sigma_px = (smooth_um / um_per_px) if um_per_px else smooth_um
    if sigma_px < 0.5:
        return stain
    return cv2.GaussianBlur(stain, (0, 0), float(sigma_px))


def threshold_in_mask(stain: np.ndarray, mask: np.ndarray, method: str,
                      scale: float = 1.0,
                      od_threshold: float = 0.0) -> tuple[np.ndarray, float]:
    """Binarize the stain map inside the well mask. Returns (binary, threshold).

    ``scale`` is the detection-sensitivity multiplier from threshold_scale():
    below 1 the threshold drops and faint colonies survive, above 1 only the
    darkest cores do. 1.0 is the historical behaviour. It applies to the
    automatic methods only -- a "fixed" threshold that a slider could move
    per run would still be one number for the whole run, but calling it fixed
    and then scaling it invites exactly the confusion this mode exists to end.

    ``method="fixed"`` uses ``od_threshold`` verbatim on every plate. Otsu and
    adaptive both derive their cut from the plate in front of them, which makes
    each plate its own reference -- fine for reading one image, wrong for a
    dose-response, where the whole question is how plates compare to each other.
    The stain map in ``od`` mode is an optical density against the well's own
    bright floor, i.e. a physical quantity on a common scale, so one absolute
    level means the same thing on a sparse plate and a dense one.
    """
    m = mask > 0
    vals = stain[m]
    if vals.size == 0 or float(vals.max()) <= 0:
        return np.zeros(stain.shape, bool), 0.0

    if method == "fixed":
        t = float(od_threshold)
        return (stain > t) & m, t

    if method == "adaptive":
        norm = np.zeros(stain.shape, np.uint8)
        vmax = float(vals.max())
        norm[m] = np.clip(stain[m] / vmax * 255.0, 0, 255).astype(np.uint8)
        blk = max(3, (min(stain.shape) // 20) | 1)  # odd block ~5% of image
        # Adaptive has no single threshold to scale, so sensitivity moves the
        # constant instead: the margin a pixel must clear above its local mean.
        binary = cv2.adaptiveThreshold(norm, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, blk, -2.0 * scale) > 0
        return binary & m, float("nan")

    # Otsu computed on in-mask values only (skimage's histogram-based otsu).
    from skimage.filters import threshold_otsu
    t = float(threshold_otsu(vals)) * float(scale)
    return (stain > t) & m, t


def segment_colonies(binary: np.ndarray, split_sensitivity: float) -> np.ndarray:
    """Distance-transform + h-maxima marker-controlled watershed.

    h-maxima seeds one marker per dome in the distance transform: a larger
    ``split_sensitivity`` (h) suppresses shallow domes so big colonies are not
    over-split, while still separating touching colonies in a cluster. Every
    connected component is guaranteed at least one marker so nothing is dropped.
    """
    if not binary.any():
        return np.zeros(binary.shape, np.int32)

    dist = ndi.distance_transform_edt(binary)
    markers = ndi.label(h_maxima(dist, split_sensitivity))[0]

    comp, ncomp = ndi.label(binary)
    seeded = np.zeros(ncomp + 1, bool)
    seeded[comp[markers > 0]] = True
    missing = [c for c in range(1, ncomp + 1) if not seeded[c]]
    if missing:
        peaks = ndi.maximum_position(dist, comp, missing)
        if isinstance(peaks, tuple):  # scipy returns a bare tuple for one label
            peaks = [peaks]
        nxt = int(markers.max())
        for (yy, xx) in peaks:
            nxt += 1
            markers[yy, xx] = nxt

    return watershed(-dist, markers, mask=binary).astype(np.int32)


# ----------------------------------------------------------------------------
# Measurement + filtering
# ----------------------------------------------------------------------------
def measure_and_filter(labels: np.ndarray, stain: np.ndarray, mask: np.ndarray,
                       um_per_px: float | None, min_colony_um: float,
                       min_solidity: float) -> tuple[list[dict], list[int], list[int]]:
    """Return (kept_rows, kept_label_ids, dropped_label_ids).

    Drops objects below the real-diameter floor, touching the well boundary, or
    below the solidity gate (scratches / hairs / dust). No upper size cap.
    """
    # minimum area from a real diameter; with no scale, treat the value as px.
    if um_per_px:
        min_diam_px = min_colony_um / um_per_px
    else:
        min_diam_px = min_colony_um
    min_area_px = np.pi * (min_diam_px / 2.0) ** 2

    # 1px boundary ring of the analysis mask; labels touching it are partial.
    eroded = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    border_ids = set(np.unique(labels[(mask > 0) & (eroded == 0)]))

    kept_rows: list[dict] = []
    kept_ids: list[int] = []
    dropped_ids: list[int] = []

    for p in regionprops(labels, intensity_image=stain):
        if p.label in border_ids:
            dropped_ids.append(p.label)
            continue
        if p.area < min_area_px:
            dropped_ids.append(p.label)
            continue
        if p.solidity < min_solidity:
            dropped_ids.append(p.label)
            continue

        cy, cx = p.centroid  # (row, col)
        area_px = float(p.area)
        # computed from area / intensity directly to avoid skimage property
        # renames across versions (equivalent_diameter -> *_area, mean_intensity
        # -> intensity_mean).
        equiv_diam_px = 2.0 * np.sqrt(area_px / np.pi)
        region_vals = p.intensity_image[p.image]
        mean_stain = float(region_vals.mean()) if region_vals.size else 0.0
        integrated_stain = float(region_vals.sum())
        if um_per_px:
            mm_per_px = um_per_px / 1000.0
            area_mm2 = area_px * mm_per_px ** 2
            equiv_diam_um = equiv_diam_px * um_per_px
        else:
            area_mm2 = float("nan")
            equiv_diam_um = float("nan")

        kept_ids.append(p.label)
        kept_rows.append({
            "label": int(p.label),
            "centroid_x": float(cx),
            "centroid_y": float(cy),
            "area_px": area_px,
            "area_mm2": area_mm2,
            "equiv_diam_um": equiv_diam_um,
            "mean_stain": mean_stain,
            "integrated_stain": integrated_stain,
            "solidity": float(p.solidity),
        })

    return kept_rows, kept_ids, dropped_ids


# ----------------------------------------------------------------------------
# Overlay (manual-validation artefact)
# ----------------------------------------------------------------------------
def _to_display(bgr: np.ndarray) -> np.ndarray:
    if bgr.ndim == 2:
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    if bgr.dtype != np.uint8:
        bgr = np.clip(bgr.astype(np.float32) / (np.iinfo(bgr.dtype).max / 255.0
                      if np.issubdtype(bgr.dtype, np.integer) else 1.0),
                      0, 255).astype(np.uint8)
    return bgr.copy()


def render_overlay(bgr: np.ndarray, labels: np.ndarray, kept_rows: list[dict],
                   kept_ids: list[int], dropped_ids: list[int],
                   well_radius_px: float, header: str, out_path: Path,
                   well_centre: tuple[float, float] | None = None,
                   mask_radius_px: float | None = None) -> None:
    ov = _to_display(bgr)
    h, w = ov.shape[:2]
    thick = max(2, int(well_radius_px * 0.004))
    fs = max(0.5, well_radius_px * 0.0011)

    # The well circle and the analysis mask, drawn because "open overlays/ and
    # check" is the whole quality-control story for this assay, and a mask that
    # has slipped off the well is the failure that costs the most and shows the
    # least in a number.
    if well_centre is not None:
        cx, cy = int(round(well_centre[0])), int(round(well_centre[1]))
        # Amber, because the stain is blue-violet and the colony outlines are
        # green: the one colour left that cannot be mistaken for data.
        cv2.circle(ov, (cx, cy), int(round(well_radius_px)), (0, 170, 255),
                   max(1, thick // 2))
        if mask_radius_px:
            cv2.circle(ov, (cx, cy), int(round(mask_radius_px)), (0, 110, 255),
                       max(1, thick // 2))
        cv2.drawMarker(ov, (cx, cy), (0, 170, 255), cv2.MARKER_CROSS,
                       int(well_radius_px * 0.06), max(1, thick // 2))

    # filtered-out objects: faint thin gray, so over/under-segmentation shows
    if dropped_ids:
        drop_lab = np.where(np.isin(labels, dropped_ids), labels, 0)
        ov[find_boundaries(drop_lab, mode="outer")] = (120, 120, 120)

    # kept colonies: bright green outline + number at centroid
    if kept_ids:
        keep_lab = np.where(np.isin(labels, kept_ids), labels, 0)
        ov[find_boundaries(keep_lab, mode="outer")] = (0, 255, 0)
        for i, row in enumerate(kept_rows, start=1):
            cx, cy = int(round(row["centroid_x"])), int(round(row["centroid_y"]))
            cv2.putText(ov, str(i), (cx + 3, cy - 3), cv2.FONT_HERSHEY_SIMPLEX,
                        fs, (0, 0, 255), max(1, thick // 2), cv2.LINE_AA)

    # header band
    pad = int(0.012 * h)
    band = max(40, int(0.045 * h))
    cv2.rectangle(ov, (0, 0), (w, band), (0, 0, 0), -1)
    cv2.putText(ov, header, (pad, int(band * 0.7)), cv2.FONT_HERSHEY_SIMPLEX,
                max(0.7, well_radius_px * 0.0016), (255, 255, 255),
                max(2, thick), cv2.LINE_AA)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), ov)


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
def per_condition_rows(plate_rows: list[dict]) -> list[dict]:
    if not plate_rows:
        return []
    df = pd.DataFrame(plate_rows)
    out = []
    for cond in sorted(df["condition"].unique()):
        c = df[df["condition"] == cond]
        out.append({
            "condition": cond,
            "n_plates": int(len(c)),
            "colony_count_mean": float(c["colony_count"].mean()),
            "colony_count_sd": float(c["colony_count"].std(ddof=1))
                                if len(c) > 1 else 0.0,
            "mean_area_mm2": float(c["mean_area_mm2"].mean()),
            # Stained area per condition. With a fixed threshold this is the
            # readout that survives colonies growing into each other: once two
            # colonies merge the COUNT is capped and no algorithm recovers the
            # two, but the area they cover is still measured correctly.
            "stained_fraction_mean": float(c["stained_fraction"].mean()),
            "stained_fraction_sd": float(c["stained_fraction"].std(ddof=1))
                                    if len(c) > 1 else 0.0,
            "n_confluent_plates": int(c["confluent"].sum()),
        })
    return out


def _round4(df: pd.DataFrame) -> pd.DataFrame:
    return df.round({col: 4 for col in df.select_dtypes(include="number").columns})


def _options_note(opts) -> str:
    """The run's knobs as one line, for run_info.

    They were previously only in log.txt prose, which means a workbook on its
    own could not say what threshold produced its counts — and with the default
    otsu mode every plate is its own reference, so that is not a detail.
    """
    keys = [f.name for f in _dc_fields(CountingOptions)]
    parts = []
    for k in keys:
        v = getattr(opts, k, None)
        if v is not None:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def write_outputs(out_dir: Path, all_colony_rows: list[dict],
                  plate_rows: list[dict], write_log=None,
                  options_note: str = "") -> tuple[int, int]:
    """Write the xlsx (per_colony / per_plate / per_condition) + summary CSV.

    Returns (n_plates, n_colonies). The three original sheets and the summary
    CSV are exactly what they were; the report layer adds condition_summary, qc,
    two figures and the explorer beside them.

    ``write_log`` is optional so the CLI path keeps working without one — its
    messages then go to the module logger instead of log.txt.
    """
    cond_rows = per_condition_rows(plate_rows)
    if write_log is None:
        def write_log(msg: str) -> None:                    # noqa: F811
            log.info("[counting] %s", msg)

    colony_cols = ["condition", "plate", "label", "centroid_x", "centroid_y",
                   "area_px", "area_mm2", "equiv_diam_um", "mean_stain",
                   "integrated_stain", "solidity"]
    plate_cols = ["condition", "plate", "colony_count", "mean_area_mm2",
                  "median_area_mm2", "total_colony_area_mm2", "stained_fraction",
                  "confluent", "um_per_px", "well_radius_px", "well_source",
                  "cells_seeded", "image_path"]
    cond_cols = ["condition", "n_plates", "colony_count_mean", "colony_count_sd",
                 "mean_area_mm2", "stained_fraction_mean", "stained_fraction_sd",
                 "n_confluent_plates"]

    colony_df = _round4(pd.DataFrame(all_colony_rows, columns=colony_cols))
    plate_df = _round4(pd.DataFrame(plate_rows, columns=plate_cols))
    cond_df = _round4(pd.DataFrame(cond_rows, columns=cond_cols))

    with pd.ExcelWriter(out_dir / "counting_results.xlsx", engine="openpyxl") as xw:
        colony_df.to_excel(xw, sheet_name="per_colony", index=False)
        plate_df.to_excel(xw, sheet_name="per_plate", index=False)
        cond_df.to_excel(xw, sheet_name="per_condition", index=False)
        try:
            # counting.py doubles as a standalone CLI, in which case sys.path[0]
            # is this folder and launcher/ is not importable. Add it here rather
            # than letting the report layer silently never run from the CLI.
            import sys as _sys
            _launcher = str(Path(__file__).resolve().parent.parent)
            if _launcher not in _sys.path:
                _sys.path.insert(0, _launcher)
            import assay_reports
            assay_reports.counting_report(xw.book, plate_rows, all_colony_rows,
                                          out_dir, write_log,
                                          options_note=options_note)
        except Exception as exc:                              # noqa: BLE001
            log.warning("counting: report layer failed", exc_info=True)
            write_log(f"WARNING: condition_summary, qc, the figures and the "
                      f"explorer could not be written ({exc}). per_colony, "
                      "per_plate, per_condition and the overlays are "
                      "unaffected.")
    cond_df.to_csv(out_dir / "counting_summary.csv", index=False)

    return len(plate_rows), len(all_colony_rows)


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------
def process_image(path: Path, root: Path, opts, out_dir: Path,
                  write_log) -> tuple[list[dict], dict | None]:
    condition, plate = resolve_image_path(path, root)
    tag = f"{condition}/{plate}/{path.name}"

    bgr, tags = read_image(path)
    if bgr is None:
        write_log(f"{tag}: could not read - skipped")
        return [], None

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    found = well_circle(gray, opts, tag, write_log)
    if found is None:
        return [], None
    cx, cy, r, well_src = found

    mask = np.zeros(gray.shape, np.uint8)
    cv2.circle(mask, (int(round(cx)), int(round(cy))),
               int(round(r * opts.mask_shrink)), 255, -1)
    mask_area = int((mask > 0).sum())

    um_per_px, scale_src = compute_scale(r, tags, opts.well_diameter_mm)
    bg_radius_px = (opts.background_radius_um / um_per_px) if um_per_px else (0.4 * r)

    stain = build_stain_map(bgr, mask, opts.stain_channel)
    stain = flatten(stain, bg_radius_px)

    # Detection map: optionally blurred so feathery colonies read as one object.
    # Measurement still uses the unsmoothed `stain` below.
    smooth_um = float(getattr(opts, "smooth_um", 0.0) or 0.0)
    detect = smooth_stain(stain, smooth_um, um_per_px)
    scale = threshold_scale(getattr(opts, "sensitivity", _SENS_NEUTRAL))
    binary, thr = threshold_in_mask(detect, mask, opts.threshold, scale,
                                    getattr(opts, "od_threshold", 0.0))

    stained_px = int((binary & (mask > 0)).sum())
    stained_fraction = stained_px / mask_area if mask_area else 0.0
    confluent = stained_fraction > opts.confluence_frac

    labels = segment_colonies(binary, opts.split_sensitivity)
    colony_rows, kept_ids, dropped_ids = measure_and_filter(
        labels, stain, mask, um_per_px, opts.min_colony_um, opts.min_solidity)

    for row in colony_rows:
        row["condition"] = condition
        row["plate"] = plate

    header = (f"{condition} | {plate}   count={len(kept_ids)}   "
              f"confluent={confluent}   stained={stained_fraction:.2f}   "
              f"well={well_src}")
    render_overlay(bgr, labels, colony_rows, kept_ids, dropped_ids, r, header,
                   out_dir / "overlays" / f"{condition}__{plate}.png",
                   well_centre=(cx, cy), mask_radius_px=r * opts.mask_shrink)

    areas_mm2 = [row["area_mm2"] for row in colony_rows]
    plate_row = {
        "condition": condition,
        "plate": plate,
        "colony_count": len(kept_ids),
        "mean_area_mm2": float(np.nanmean(areas_mm2)) if areas_mm2 else float("nan"),
        "median_area_mm2": float(np.nanmedian(areas_mm2)) if areas_mm2 else float("nan"),
        "total_colony_area_mm2": float(np.nansum(areas_mm2)) if areas_mm2 else float("nan"),
        "stained_fraction": stained_fraction,
        "confluent": confluent,
        "um_per_px": um_per_px if um_per_px else float("nan"),
        "well_radius_px": float(r),
        "well_source": well_src,
        "cells_seeded": float("nan"),  # hook for later
        "image_path": str(path),
    }

    thr_str = "adaptive" if isinstance(thr, float) and np.isnan(thr) else f"{thr:.4f}"
    thr_note = "fixed" if opts.threshold == "fixed" else f"x{scale:.2f}"
    write_log(
        f"{tag}: well={well_src} r={r:.0f}px scale={scale_src} "
        f"bg_r={bg_radius_px:.0f}px "
        f"smooth={smooth_um:.0f}um thr={thr_str} ({thr_note}) "
        f"n_raw={len(kept_ids) + len(dropped_ids)} n_kept={len(kept_ids)} "
        f"stained={stained_fraction:.3f} confluent={confluent}"
    )
    return colony_rows, plate_row


def run(experiment_dir: str, output_dir: str | None, opts) -> Path:
    root = Path(experiment_dir)
    if output_dir:
        out_dir = Path(output_dir)
    else:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_dir = root / f"{_ANALYSIS_PREFIX}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    images = find_images(root, opts.max_depth)
    all_colony_rows: list[dict] = []
    plate_rows: list[dict] = []

    with open(out_dir / "log.txt", "w", encoding="utf-8") as lf:
        def write_log(msg: str) -> None:
            lf.write(msg + "\n")
            lf.flush()
            print(msg)

        write_log(f"counting: {len(images)} image(s) under {root}")
        for path in images:
            colony_rows, plate_row = process_image(path, root, opts, out_dir, write_log)
            all_colony_rows.extend(colony_rows)
            if plate_row is not None:
                plate_rows.append(plate_row)

        n_plates, n_colonies = write_outputs(
            out_dir, all_colony_rows, plate_rows, write_log,
            options_note=_options_note(opts))

        write_log(f"done: {n_plates} plate(s), "
                  f"{n_colonies} colony(ies) -> {out_dir}")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Count crystal-violet colonies in single-well plate stills.")
    ap.add_argument("experiment_dir")
    ap.add_argument("-o", "--output_dir", default=None,
                    help="default: <experiment_dir>/_counting_analysis_<timestamp>/")
    # prominent tuning knobs
    ap.add_argument("--split-sensitivity", type=float, default=3.0,
                    help="h-maxima depth; higher = fewer splits (default 3.0)")
    ap.add_argument("--min-colony-um", type=float, default=200.0,
                    help="minimum colony diameter in um (default 200)")
    ap.add_argument("--sensitivity", type=float, default=_SENS_NEUTRAL,
                    help="detection sensitivity 0-10; 5 = automatic threshold "
                         "unchanged, higher picks up fainter/sparser colonies "
                         "(default 5)")
    ap.add_argument("--smooth-um", type=float, default=0.0,
                    help="blur the detection map at this scale in um before "
                         "thresholding, so feathery colonies are one object "
                         "instead of many fragments; 0 = off (default 0)")
    # scale + stain
    ap.add_argument("--well-diameter-mm", type=float, default=34.8,
                    help="physical well diameter; 0 = use TIFF tags (default 34.8)")
    ap.add_argument("--stain-channel", choices=["green", "od", "gray"], default="od")
    ap.add_argument("--threshold", choices=["otsu", "adaptive", "fixed"],
                    default="otsu",
                    help="otsu/adaptive derive the cut per plate; fixed applies "
                         "--od-threshold to every plate, which is what makes "
                         "counts comparable across a dose series (default otsu)")
    ap.add_argument("--od-threshold", type=float, default=0.0,
                    help="absolute stain (optical density) cut used by "
                         "--threshold fixed; ignored otherwise")
    ap.add_argument("--background-radius-um", type=float, default=3000.0,
                    help="white-tophat kernel radius in um; must exceed the "
                         "largest colony so it is not hollowed (default 3000)")
    # secondary filters
    ap.add_argument("--min-solidity", type=float, default=0.5)
    ap.add_argument("--confluence-frac", type=float, default=0.55)
    ap.add_argument("--mask-shrink", type=float, default=0.915,
                    help="analysis-mask radius as a fraction of the well "
                         "radius; the margin that keeps the mask inside the "
                         "well when framing is imperfect (default 0.915)")
    ap.add_argument("--well-mode", choices=["aim", "auto"], default="aim",
                    help="aim: the well is the capture UI's aim circle — "
                         "centred, radius --aim-circle-frac x the short side "
                         "(default). auto: detect the well circle in the image, "
                         "for stills that were not framed to the aim circle")
    ap.add_argument("--aim-circle-frac", type=float, default=_AIM_CIRCLE_FRAC,
                    help="aim-circle radius as a fraction of the frame's short "
                         "side; must match GUIDED_CIRCLE_FRAC in the capture UI "
                         f"(default {_AIM_CIRCLE_FRAC})")
    ap.add_argument("--max-depth", type=int, default=_MAX_DEPTH)
    args = ap.parse_args()

    run(args.experiment_dir, args.output_dir, args)


if __name__ == "__main__":
    main()
