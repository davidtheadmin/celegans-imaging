#!/usr/bin/env python3
"""
capture.py -- Full-resolution image capture for C. elegans microscopy.

Camera: Sony IMX477 HQ Camera (12.3 MP) via Picamera2
Sensor: 4056 x 3040 pixels, 10/12-bit Bayer RGGB

Flat-field correction compensates for uneven illumination across the
microscope field of view -- a common artefact with transmitted-light and
epi-fluorescence setups.  The workflow is:

    1. Capture a flat-field reference (empty slide, full illumination):
           python3 capture.py --capture-flat

    2. Capture worm images with correction applied on-the-fly:
           python3 capture.py --count 10 --correct

    3. Or capture raw frames and correct in post-processing:
           python3 capture.py --count 10
           python3 capture.py --correct-dir ../data/raw

Directory layout (relative to this script):
    ../data/raw/          Raw timestamped PNGs
    ../data/flatfield/    Flat-field reference frames
    ../data/processed/    Flat-field-corrected output PNGs
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
from picamera2 import Picamera2

# ---------------------------------------------------------------------------
# Paths -- everything relative to this script so the project is portable
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
RAW_DIR     = PROJECT_DIR / "data" / "raw"
FF_DIR      = PROJECT_DIR / "data" / "flatfield"
PROC_DIR    = PROJECT_DIR / "data" / "processed"

# Filename of the master flat kept in FF_DIR
MASTER_FLAT_NAME = "master_flat.npy"   # saved as float32 numpy array

# ---------------------------------------------------------------------------
# Camera settings
# ---------------------------------------------------------------------------
# Full-sensor resolution for the IMX477 (mode 4/9 in sensor_modes).
# 4056x3040 gives the complete 12.3 MP field of view.
FULL_WIDTH  = 4056
FULL_HEIGHT = 3040

# Frames to average when building the master flat.
# More frames -> less shot-noise in the reference, but takes longer.
FLAT_N_FRAMES = 16


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ===========================================================================
# Camera helpers
# ===========================================================================

def make_camera(exposure_us=None, gain=None):
    """
    Initialise Picamera2 for full-resolution still capture.

    Parameters
    ----------
    exposure_us : int or None
        Shutter speed in microseconds.  None -> auto-exposure.
    gain : float or None
        Analogue gain (>= 1.0).  None -> auto-gain.

    Returns
    -------
    Picamera2
        Configured and started camera instance.  Call .close() when done.
    """
    cam = Picamera2()

    # create_still_configuration requests the highest-quality ISP pipeline.
    # main stream : RGB888 output at full resolution -> what we save as PNG.
    # raw  stream : keeps the Bayer data available should you later want to
    #               do your own demosaicing or write DNG files.
    cfg = cam.create_still_configuration(
        main={"size": (FULL_WIDTH, FULL_HEIGHT), "format": "RGB888"},
        raw={"size": (FULL_WIDTH, FULL_HEIGHT)},
    )
    cam.configure(cfg)

    # Build the control dict for manual / semi-manual exposure.
    controls = {}
    if exposure_us is not None:
        # FrameDurationLimits prevents the camera from extending the frame
        # period beyond the requested exposure (which AE would otherwise do).
        controls["ExposureTime"]        = int(exposure_us)
        controls["FrameDurationLimits"] = (int(exposure_us), int(exposure_us))
        controls["AeEnable"]            = False   # disable auto-exposure
    if gain is not None:
        controls["AnalogueGain"] = float(gain)
        controls["AeEnable"]     = False

    if controls:
        cam.set_controls(controls)

    cam.start()

    # Allow the AGC/AWB to settle before the first capture.
    # With fully manual settings a shorter settle time is fine.
    settle_s = 0.5 if controls else 2.0
    time.sleep(settle_s)

    return cam


def capture_array(cam):
    """
    Capture a single frame and return it as a uint8 RGB numpy array
    of shape (FULL_HEIGHT, FULL_WIDTH, 3).
    """
    return cam.capture_array("main")


def timestamp():
    """Return an ISO-8601-style timestamp string safe for filenames."""
    # Format: 20260324T143201_456  (date T time _ milliseconds)
    return datetime.now().strftime("%Y%m%dT%H%M%S_%f")[:-3]


def save_png(array, path):
    """Save a uint8 RGB numpy array as a lossless PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array, mode="RGB").save(path, format="PNG", optimize=False)
    log.info("Saved  %s  (%d x %d)", path.name, array.shape[1], array.shape[0])


# ===========================================================================
# Flat-field correction
# ===========================================================================

def build_master_flat(cam, n_frames=FLAT_N_FRAMES):
    """
    Build a master flat-field reference by averaging n_frames frames.

    Averaging suppresses photon (shot) noise so the reference does not add
    noise to corrected science frames.  The result is normalised channel-wise
    so the mean pixel value per channel is 1.0, making the correction a
    simple per-pixel division.

    Parameters
    ----------
    cam : Picamera2
        Running camera instance aimed at a uniform, empty illumination field
        (blank slide or open field -- NO sample present).
    n_frames : int
        Number of frames to average.

    Returns
    -------
    np.ndarray
        float32 array shape (H, W, 3), values centred around 1.0.
    """
    log.info("Capturing %d flat-field frames to average ...", n_frames)
    accumulator = np.zeros((FULL_HEIGHT, FULL_WIDTH, 3), dtype=np.float64)

    for i in range(n_frames):
        frame = capture_array(cam).astype(np.float64)
        accumulator += frame
        log.info("  flat frame %d / %d", i + 1, n_frames)

    master = (accumulator / n_frames).astype(np.float32)

    # Normalise each channel independently so that dividing a science frame
    # by the flat preserves overall brightness AND removes illumination
    # colour cast (useful for brightfield and some fluorescence setups).
    for ch in range(3):
        ch_mean = master[:, :, ch].mean()
        if ch_mean > 0:
            master[:, :, ch] /= ch_mean
        else:
            log.warning("Flat channel %d has zero mean -- skipping normalisation", ch)

    return master


def save_master_flat(flat, out_dir=FF_DIR):
    """
    Save the master flat as a .npy file (lossless float32) and export a
    preview PNG for quick visual inspection.

    Parameters
    ----------
    flat : np.ndarray
        float32 array from build_master_flat().
    out_dir : Path
        Destination directory (default: data/flatfield/).

    Returns
    -------
    Path
        Path to the saved .npy file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    npy_path = out_dir / MASTER_FLAT_NAME

    np.save(npy_path, flat)
    log.info("Master flat saved -> %s", npy_path)

    # Write a uint8 PNG scaled to [0, 255] for visual inspection.
    # Bright regions in the preview = areas where the sensor responds more
    # strongly (will be divided down in corrected images).
    vis = np.clip(flat / flat.max() * 255, 0, 255).astype(np.uint8)
    png_path = out_dir / "master_flat_preview.png"
    Image.fromarray(vis, mode="RGB").save(png_path)
    log.info("Flat preview  -> %s", png_path)

    return npy_path


def load_master_flat(ff_dir=FF_DIR):
    """
    Load the master flat from disk.

    Parameters
    ----------
    ff_dir : Path
        Directory containing MASTER_FLAT_NAME (default: data/flatfield/).

    Returns
    -------
    np.ndarray
        float32 flat array of shape (H, W, 3).

    Raises
    ------
    FileNotFoundError
        If no master flat has been captured yet.
    """
    npy_path = ff_dir / MASTER_FLAT_NAME
    if not npy_path.exists():
        raise FileNotFoundError(
            f"No master flat found at {npy_path}.\n"
            "Capture one first:  python3 capture.py --capture-flat"
        )
    flat = np.load(npy_path)
    log.info("Loaded master flat from %s  (shape %s)", npy_path, flat.shape)
    return flat


def apply_flat_field(image, flat):
    """
    Apply flat-field correction to a science frame.

    Correction formula (applied per channel, per pixel):

        corrected[c] = image[c] / flat[c]

    Because flat is normalised to mean ~= 1.0 per channel, division preserves
    average brightness while equalising the spatial illumination response.
    Pixels in dim regions of the flat are boosted; bright regions are
    attenuated -- the net effect is a spatially uniform apparent illumination.

    Parameters
    ----------
    image : np.ndarray
        uint8 RGB array of shape (H, W, 3) -- the raw science frame.
    flat : np.ndarray
        float32 RGB array of shape (H, W, 3) -- the master flat (mean ~= 1.0).

    Returns
    -------
    np.ndarray
        uint8 RGB corrected array of shape (H, W, 3).

    Raises
    ------
    ValueError
        If image and flat shapes do not match.
    """
    if image.shape != flat.shape:
        raise ValueError(
            f"Image shape {image.shape} does not match flat shape {flat.shape}. "
            "Re-capture the flat-field reference at the same resolution."
        )

    # Cast to float32 before division to avoid integer overflow / truncation.
    corrected = image.astype(np.float32) / flat

    # Clip to valid uint8 range and convert back.
    # Values >255 indicate the raw pixel was brighter than the flat reference
    # at that location, which can happen if the sample is more reflective
    # than the blank field.  Clipping is safe for display/ML use; if you
    # need radiometric accuracy, store corrected as float32 instead.
    return np.clip(corrected, 0.0, 255.0).astype(np.uint8)


def correct_directory(src_dir, flat, dst_dir=PROC_DIR):
    """
    Batch-correct all PNGs in src_dir and write results to dst_dir.

    Parameters
    ----------
    src_dir : Path
        Directory containing raw PNG images.
    flat : np.ndarray
        Master flat array (from load_master_flat()).
    dst_dir : Path
        Destination directory for corrected PNGs.
    """
    png_files = sorted(src_dir.glob("*.png"))
    if not png_files:
        log.warning("No PNG files found in %s", src_dir)
        return

    dst_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "Correcting %d images  %s -> %s",
        len(png_files), src_dir, dst_dir,
    )

    for src_path in png_files:
        img = np.array(Image.open(src_path).convert("RGB"))
        corrected = apply_flat_field(img, flat)
        dst_path = dst_dir / src_path.name
        save_png(corrected, dst_path)


# ===========================================================================
# Main capture routine
# ===========================================================================

def capture_images(count, exposure_us, gain, apply_correction, interval_s):
    """
    Capture count full-resolution frames and save them as timestamped PNGs.

    Parameters
    ----------
    count : int
        Number of frames to capture.
    exposure_us : int or None
        Manual shutter speed (us), or None for auto.
    gain : float or None
        Manual analogue gain, or None for auto.
    apply_correction : bool
        If True, apply flat-field correction and save to PROC_DIR.
        Raw frames are always saved to RAW_DIR regardless.
    interval_s : float
        Minimum seconds between frames (0 = as fast as possible).
    """
    flat = None
    if apply_correction:
        flat = load_master_flat()   # raises FileNotFoundError if absent

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if apply_correction:
        PROC_DIR.mkdir(parents=True, exist_ok=True)

    cam = make_camera(exposure_us=exposure_us, gain=gain)
    try:
        for i in range(count):
            t_start = time.monotonic()

            ts = timestamp()
            raw_path = RAW_DIR / f"frame_{ts}.png"

            frame = capture_array(cam)
            save_png(frame, raw_path)

            if flat is not None:
                corrected = apply_flat_field(frame, flat)
                proc_path = PROC_DIR / f"frame_{ts}_corrected.png"
                save_png(corrected, proc_path)

            log.info("Captured %d / %d", i + 1, count)

            # Honour the requested inter-frame interval.
            if count > 1 and interval_s > 0:
                elapsed = time.monotonic() - t_start
                sleep_for = max(0.0, interval_s - elapsed)
                if sleep_for > 0:
                    time.sleep(sleep_for)
    finally:
        # Always close the camera, even if an exception is raised.
        cam.close()


# ===========================================================================
# CLI
# ===========================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Capture full-resolution C. elegans images with the IMX477 HQ Camera.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--capture-flat",
        action="store_true",
        help=(
            "Capture a flat-field reference (blank slide, full illumination). "
            f"Averages {FLAT_N_FRAMES} frames and saves to data/flatfield/."
        ),
    )
    mode.add_argument(
        "--correct-dir",
        metavar="DIR",
        type=Path,
        help="Batch-correct all PNGs in DIR using the saved master flat.",
    )

    p.add_argument(
        "--count", "-n",
        type=int, default=1,
        help="Number of frames to capture (default: 1).",
    )
    p.add_argument(
        "--interval", "-i",
        type=float, default=0.0,
        metavar="SECONDS",
        help="Minimum interval between frames in seconds (default: 0).",
    )
    p.add_argument(
        "--correct", "-c",
        action="store_true",
        help="Apply flat-field correction to each frame as it is captured.",
    )
    p.add_argument(
        "--exposure", "-e",
        type=int, default=None,
        metavar="MICROSECONDS",
        help="Manual shutter speed in microseconds (omit for auto-exposure).",
    )
    p.add_argument(
        "--gain", "-g",
        type=float, default=None,
        help="Manual analogue gain >= 1.0 (omit for auto-gain).",
    )
    p.add_argument(
        "--flat-frames",
        type=int, default=FLAT_N_FRAMES,
        metavar="N",
        help=f"Frames to average when building the master flat (default: {FLAT_N_FRAMES}).",
    )
    return p.parse_args()


def main():
    args = parse_args()

    if args.capture_flat:
        # ---- Flat-field acquisition mode ------------------------------------
        log.info("=== Flat-field capture mode ===")
        log.info(
            "Point the camera at a uniform blank field (empty slide or diffuser) "
            "with illumination set to the level used for normal imaging."
        )
        cam = make_camera(exposure_us=args.exposure, gain=args.gain)
        try:
            flat = build_master_flat(cam, n_frames=args.flat_frames)
        finally:
            cam.close()
        save_master_flat(flat)

    elif args.correct_dir is not None:
        # ---- Batch post-correction mode -------------------------------------
        log.info("=== Batch flat-field correction mode ===")
        flat = load_master_flat()
        correct_directory(args.correct_dir, flat)

    else:
        # ---- Normal capture mode --------------------------------------------
        log.info(
            "=== Capture mode: %d frame(s) at %d x %d ===",
            args.count, FULL_WIDTH, FULL_HEIGHT,
        )
        capture_images(
            count=args.count,
            exposure_us=args.exposure,
            gain=args.gain,
            apply_correction=args.correct,
            interval_s=args.interval,
        )

    log.info("Done.")


if __name__ == "__main__":
    main()
