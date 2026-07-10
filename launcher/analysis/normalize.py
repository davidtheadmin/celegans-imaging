"""Scale normalization for the YOLO staging pipeline.

Resamples every image to a single canonical um/px so apparent worm/egg size is
constant regardless of acquisition magnification. The SAME pixel transform runs
at training-prep time (output -> Roboflow) and at inference time; both import
``resample_to_canonical`` so the two paths can never diverge.

CANONICAL IS A FLOOR (not a target you approach from both sides). The canonical
value in ``canonical_scale.json`` is the COARSEST acceptable magnification -- the
scale below which worms/eggs can no longer be reliably counted. Legitimate images
are captured at this scale or finer, so they reach canonical by DOWNSCALING. An
image coarser than canonical would require UPSCALING (inventing detail that was
never captured), which degrades detection of the smallest targets (eggs, L1) and
is therefore REFUSED by default rather than resampled.

Calibration I/O reuses the existing helpers so tag parsing is not reinvented:
  - ``crop_wells._read``   -- TIFF read + resolution/description tags (BGR order)
  - ``counting._rational`` -- parse a TIFF RATIONAL (num, den) -> float
The um/px convention is the one written by ``capture/app/capture_ops.py``:
ResolutionUnit=1 (none), XResolution = pixels-per-micron as a RATIONAL, and the
ImageDescription carries ``unit=um``; um/px = 1 / XResolution.

CHANNEL ORDER: ``crop_wells._read`` returns BGR for 3-channel images (OpenCV
order). This module standardizes to RGB via ``_read_rgb`` before resampling and
writing, so the array handed to Roboflow/YOLO matches the PNG on disk. The
inference path MUST read through ``_read_rgb`` (or apply the same BGR->RGB
conversion) so both paths agree on channel order.

CLI (training prep):
    python normalize.py <in_dir> -o <out_dir> [--write-tiff]
                        [--on-below-floor {refuse,warn}]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:  # run as a script from launcher/analysis, or as part of the package
    from crop_wells import _read as read_image
    from counting import _rational, find_images, _MAX_DEPTH
except ImportError:  # pragma: no cover - import path shim
    from analysis.crop_wells import _read as read_image
    from analysis.counting import _rational, find_images, _MAX_DEPTH

log = logging.getLogger(__name__)

# canonical_scale.json lives beside this module; both training-prep and
# inference load the value from here so it cannot drift.
_CANONICAL_JSON = Path(__file__).with_name("canonical_scale.json")

# ImageJ ResolutionUnit values that mean "unit given in the description".
_IMAGEJ_UNIT_TAGS = (1, None)
_UM_UNIT_RE = re.compile(r"unit\s*=\s*(um|micron|micrometer|µm)", re.I)


class BelowFloorError(ValueError):
    """Raised when reaching canonical would require UPSCALING (source coarser
    than the minimum countable magnification floor)."""


# ----------------------------------------------------------------------------
# Canonical config
# ----------------------------------------------------------------------------
def load_canonical_um_per_px(path: Path | None = None) -> float:
    """Read canonical_um_per_px from the single JSON config (default: the file
    beside this module). Both training-prep and inference call this."""
    p = Path(path) if path is not None else _CANONICAL_JSON
    data = json.loads(p.read_text(encoding="utf-8"))
    return float(data["canonical_um_per_px"])


# ----------------------------------------------------------------------------
# The shared contract: pure resampling
# ----------------------------------------------------------------------------
def resample_to_canonical(img: np.ndarray, source_um_per_px: float,
                          canonical_um_per_px: float,
                          allow_upscale: bool = False) -> np.ndarray:
    """Resample ``img`` from ``source_um_per_px`` to ``canonical_um_per_px``.

    PURE: no file I/O, no tag reading, no printing. Deterministic. This is the
    shared contract used by BOTH training-prep and inference -- identical array
    out either path for the same inputs.

    scale = source / canonical:
      * scale < 1  (source finer than canonical): DOWNSCALE with INTER_AREA.
      * scale == 1: EXACT no-op -- the input array is returned unchanged (no
        interpolation, same object).
      * scale > 1  (source coarser -- would require UPSCALING): REFUSED by
        default with ``BelowFloorError``. Set ``allow_upscale=True`` only for
        debugging; it upscales with INTER_LANCZOS4 and never happens in normal
        operation (see the module docstring: canonical is a floor).

    The exception names both um/px values; callers that have a filename (the
    CLI) add it to their log line.
    """
    if source_um_per_px <= 0 or canonical_um_per_px <= 0:
        raise ValueError(
            f"um/px must be positive; got source={source_um_per_px}, "
            f"canonical={canonical_um_per_px}")

    scale = source_um_per_px / canonical_um_per_px

    if scale == 1.0:
        return img  # exact passthrough at the floor -- no resampling at all

    if scale > 1.0 and not allow_upscale:
        raise BelowFloorError(
            f"source {source_um_per_px:.6f} um/px is coarser than canonical "
            f"{canonical_um_per_px:.6f} um/px (scale={scale:.4f} > 1): reaching "
            f"canonical would require upscaling, but canonical is the minimum "
            f"countable magnification floor -- refusing to invent detail")

    h, w = img.shape[:2]
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


# ----------------------------------------------------------------------------
# Calibration read (reuses crop_wells._read + counting._rational)
# ----------------------------------------------------------------------------
def _um_per_px_from_tags(tags: dict | None, path) -> float:
    """um/px from ImageJ resolution tags, or hard-error. Never guesses.

    Mirrors the tag branch of ``counting.compute_scale``: ResolutionUnit must be
    1/none, the ImageDescription must carry ``unit=um``, and um/px = 1/XRes.
    """
    if not tags:
        raise ValueError(
            f"{path}: no TIFF calibration tags (not a calibrated TIFF); "
            f"refusing to guess scale")
    res = tags.get("resolution") or (None, None)
    xres = _rational(res[0])
    unit = tags.get("resolutionunit")
    desc = tags.get("description") or ""
    if xres is None or xres <= 0:
        raise ValueError(
            f"{path}: missing/invalid XResolution tag; refusing to guess scale")
    if unit not in _IMAGEJ_UNIT_TAGS:
        raise ValueError(
            f"{path}: ResolutionUnit={unit!r} (expected 1/none per the ImageJ "
            f"um convention); refusing to guess scale")
    if not _UM_UNIT_RE.search(desc):
        raise ValueError(
            f"{path}: ImageDescription lacks 'unit=um' ({desc!r}); "
            f"refusing to guess scale")
    return 1.0 / xres


def read_um_per_px(path) -> float:
    """Return um/px for a calibrated TIFF, opening it through the same reader the
    inference path uses (``crop_wells._read``). Hard-errors if any calibration
    tag is missing -- never defaults or guesses a scale."""
    _, tags = read_image(Path(path))
    return _um_per_px_from_tags(tags, path)


# ============================================================================
# !!  INFERENCE INTEGRATION INVARIANT -- READ BEFORE WIRING INFERENCE IN  !!
#
# The scale-normalization contract holds ONLY if BOTH paths (training-prep AND
# inference) feed RGB into resample_to_canonical. crop_wells._read returns BGR
# (OpenCV order). The inference path MUST read through _read_rgb below -- or
# apply the identical BGR->RGB conversion -- and must NOT hand a bare
# crop_wells._read array onward. If it does, R and B swap silently: no error is
# raised, the model just trains/infers on the wrong channels and mAP quietly
# degrades. This is the single most likely silent failure in the integration.
# ============================================================================
def _read_rgb(path) -> tuple[np.ndarray | None, dict | None]:
    """Read via ``crop_wells._read`` (the SAME reader inference uses) and return
    (rgb_uint8, tags). ``_read`` yields BGR for 3-channel images; convert to RGB
    so the array handed onward matches the PNG written for Roboflow. Both
    training-prep and inference MUST go through this conversion (see banner)."""
    img, tags = read_image(Path(path))
    if img is None:
        return None, tags
    if img.ndim == 3 and img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return img, tags


# ----------------------------------------------------------------------------
# Writers (RGB in -> correct channel order out; TIFF mirrors save_still)
# ----------------------------------------------------------------------------
def _write_png(path: Path, rgb: np.ndarray) -> None:
    """Write an RGB (or grayscale) uint8 array as a lossless PNG via PIL. PIL
    writes the given array order verbatim, so an RGB array stays RGB on disk --
    unlike cv2.imwrite, which expects BGR and would silently swap channels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGB" if rgb.ndim == 3 else "L"
    Image.fromarray(rgb, mode).save(path, format="PNG", optimize=False)


def _write_tiff_canonical(path: Path, rgb: np.ndarray,
                          canonical_um_per_px: float) -> None:
    """Write an LZW TIFF with ImageJ calibration tags for the CANONICAL scale.
    Mirrors ``capture/app/capture_ops.py::save_still`` exactly, substituting the
    canonical um/px, so the file opens pre-scaled in microns just like a native
    capture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    px_per_um = 1.0 / canonical_um_per_px
    tiffinfo = {
        282: px_per_um,                  # XResolution (pixels per unit)
        283: px_per_um,                  # YResolution
        296: 1,                          # ResolutionUnit = none; unit in description
        270: "ImageJ=1.54f\nunit=um\n",  # ImageDescription
    }
    mode = "RGB" if rgb.ndim == 3 else "L"
    Image.fromarray(rgb, mode).save(
        path, format="TIFF", compression="tiff_lzw", tiffinfo=tiffinfo)


# ----------------------------------------------------------------------------
# CLI: training-prep batch
# ----------------------------------------------------------------------------
def process_dir(in_dir, out_dir, write_tiff: bool = False,
                on_below_floor: str = "refuse", max_depth: int = _MAX_DEPTH,
                canonical_um_per_px: float | None = None) -> Path:
    """Recurse ``in_dir``, normalize every calibrated image to canonical scale,
    and write results under ``out_dir`` (mirroring the input tree).

    Per image: read um/px via the tag path, resample, write an 8-bit RGB PNG for
    Roboflow and (if ``write_tiff``) a canonical-tagged TIFF. Files missing
    calibration tags are skipped and logged (never guessed). Files below the
    floor are ALWAYS skipped without writing output -- never upscaled. Both
    ``refuse`` (default) and ``warn`` skip; they differ only in log wording.
    The CLI never upscales: ``allow_upscale`` on ``resample_to_canonical`` is a
    deliberate programmatic escape hatch and is never engaged from here.
    """
    in_root = Path(in_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if canonical_um_per_px is None:
        canonical_um_per_px = load_canonical_um_per_px()

    images = find_images(in_root, max_depth)
    n_written = n_skipped = n_refused = 0

    with open(out_dir / "log.txt", "w", encoding="utf-8") as lf:
        def write_log(msg: str) -> None:
            lf.write(msg + "\n")
            lf.flush()
            print(msg)

        write_log(f"normalize: {len(images)} image(s) under {in_root}; "
                  f"canonical={canonical_um_per_px:.6f} um/px; "
                  f"on_below_floor={on_below_floor}")

        for path in images:
            rel = path.relative_to(in_root)
            rgb, tags = _read_rgb(path)
            if rgb is None:
                write_log(f"{rel}: could not read - skipped")
                n_skipped += 1
                continue
            try:
                src_um = _um_per_px_from_tags(tags, path)
            except ValueError as e:
                write_log(f"{rel}: SKIP (no scale) - {e}")
                n_skipped += 1
                continue

            scale = src_um / canonical_um_per_px
            try:
                # No allow_upscale: below-floor files raise and are skipped
                # below. warn never stretches -- it only changes the log wording.
                out = resample_to_canonical(rgb, src_um, canonical_um_per_px)
            except BelowFloorError as e:
                label = "WARNING" if on_below_floor == "warn" else "REFUSED"
                write_log(f"{rel}: {label} (below floor; skipped, NOT written) - {e}")
                n_refused += 1
                continue

            # Only passthrough or downscale can reach here (scale <= 1).
            action = "passthrough" if scale == 1.0 else "downscaled"

            png_path = (out_dir / rel).with_suffix(".png")
            _write_png(png_path, out)
            tiff_note = ""
            if write_tiff:
                _write_tiff_canonical((out_dir / rel).with_suffix(".tif"),
                                      out, canonical_um_per_px)
                tiff_note = " +tiff"
            write_log(
                f"{rel}: src={src_um:.6f} um/px scale={scale:.4f} {action} "
                f"dims={out.shape[1]}x{out.shape[0]}{tiff_note}")
            n_written += 1

        write_log(f"done: {n_written} written, {n_skipped} skipped (no scale), "
                  f"{n_refused} refused (below floor) -> {out_dir}")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Normalize plate stills to the canonical um/px floor for "
                    "YOLO staging (writes 8-bit RGB PNGs for Roboflow).")
    ap.add_argument("in_dir")
    ap.add_argument("-o", "--out_dir", required=True,
                    help="destination for normalized PNGs (+ optional TIFFs)")
    ap.add_argument("--write-tiff", action="store_true",
                    help="also write a canonical-tagged LZW TIFF beside each PNG")
    ap.add_argument("--on-below-floor", choices=["refuse", "warn"],
                    default="refuse",
                    help="coarser-than-floor images are ALWAYS skipped (never "
                         "upscaled); refuse (default) vs warn only changes the "
                         "log wording")
    ap.add_argument("--max-depth", type=int, default=_MAX_DEPTH)
    ap.add_argument("--canonical-json", default=None,
                    help="override path to canonical_scale.json")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S")

    canonical = load_canonical_um_per_px(args.canonical_json)
    process_dir(args.in_dir, args.out_dir, write_tiff=args.write_tiff,
                on_below_floor=args.on_below_floor, max_depth=args.max_depth,
                canonical_um_per_px=canonical)


if __name__ == "__main__":
    main()
