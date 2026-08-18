"""Tests for the µm/px read and the unit decision it feeds.

Run from launcher/:  python test_survival_scale.py

Deliberately dependency-light — no pytest, same style as the parked
analysis/test_normalize.py — so it can be run on a bare launcher venv.
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import survival_scale                                    # noqa: E402
import survival_size                                     # noqa: E402

FAILURES: list[str] = []


def check(cond, what):
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        FAILURES.append(what)


def write_tiff(path: Path, um_per_px=None, resolution_unit=1, unit_str="um"):
    """Write a tiny TIFF the way capture_ops.save_still writes a real one."""
    import numpy as np
    from PIL import Image
    img = Image.fromarray(np.zeros((8, 8, 3), dtype="uint8"), "RGB")
    if um_per_px is None:
        img.save(path, format="TIFF", compression="tiff_lzw")
        return
    px_per_um = 1.0 / um_per_px
    img.save(path, format="TIFF", compression="tiff_lzw", tiffinfo={
        282: px_per_um, 283: px_per_um,
        296: resolution_unit,
        270: f"ImageJ=1.54f\nunit={unit_str}\n",
    })


def test_reading(tmp: Path):
    print("\num||px from TIFF tags")
    p = tmp / "calibrated.tif"
    write_tiff(p, um_per_px=5.0954406)
    got = survival_scale.um_per_px(p)
    check(got is not None and abs(got - 5.0954406) < 1e-3,
          f"a calibrated image reads back its own scale (got {got})")

    p = tmp / "bare.tif"
    write_tiff(p, um_per_px=None)
    check(survival_scale.um_per_px(p) is None,
          "an image with no resolution tags is None, not a default")

    p = tmp / "inch.tif"
    write_tiff(p, um_per_px=5.0, resolution_unit=2)
    check(survival_scale.um_per_px(p) is None,
          "ResolutionUnit=inch is refused rather than guessed")

    p = tmp / "wrongunit.tif"
    write_tiff(p, um_per_px=5.0, unit_str="mm")
    check(survival_scale.um_per_px(p) is None,
          "a description that does not say unit=um is refused")

    p = tmp / "absurd.tif"
    write_tiff(p, um_per_px=100000.0)
    check(survival_scale.um_per_px(p) is None,
          "an implausible scale is refused")

    check(survival_scale.um_per_px(tmp / "does_not_exist.tif") is None,
          "a missing file is None, not an exception")

    rep = survival_scale.scan([tmp / "calibrated.tif", tmp / "bare.tif"])
    check(rep.n_total == 2 and rep.n_scaled == 1 and not rep.uniform,
          "scan() counts calibrated and uncalibrated images separately")
    check("no calibration" in rep.describe(),
          "scan().describe() names the uncalibrated images")


def _fixture(tmp: Path, n=40):
    """One timepoint, one condition, two images, n detections each."""
    soft = tmp / "soft.csv"
    with open(soft, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["folder", "timepoint_h", "image", "size_px"])
        for img in ("a.tif", "b.tif"):
            for i in range(n):
                w.writerow(["f", "0", img, 40 + (i % 10)])
    per_image = [
        {"folder": "f", "timepoint_h": 0.0, "condition": "N2 0J", "strain": "N2",
         "dose": 0, "unit": "J/m²", "plate": "plate 01", "image": img}
        for img in ("a.tif", "b.tif")
    ]
    return soft, per_image


def test_units(tmp: Path):
    print("\nunit decision")
    logs: list[str] = []
    soft, per_image = _fixture(tmp)

    px = survival_size.build_size_payload(soft, per_image, logs.append)
    check(px["unit"] == "px", "no scale supplied -> pixels")
    px_med = px["groups"]["N2 0J @ 0h"]["p50"]

    scales = {("0", "a.tif"): 5.0, ("0", "b.tif"): 5.0}
    um = survival_size.build_size_payload(soft, per_image, logs.append,
                                          scale_by_key=scales)
    check(um["unit"] == "um" and um["unit_label"] == "µm",
          "every image calibrated -> micrometres")
    um_med = um["groups"]["N2 0J @ 0h"]["p50"]
    check(abs(um_med - px_med * 5.0) < 0.05,
          f"medians scale by the calibration ({px_med} px -> {um_med} µm)")
    check(abs(um["x"][0] - px["x"][0] * 5.0) < 0.5,
          "the grid scales with the sizes, so the histogram bins stay aligned")
    check(um["groups"]["N2 0J @ 0h"]["n"] == px["groups"]["N2 0J @ 0h"]["n"],
          "converting units changes no count")

    mixed = {("0", "a.tif"): 5.0, ("0", "b.tif"): None}
    mx = survival_size.build_size_payload(soft, per_image, logs.append,
                                          scale_by_key=mixed)
    check(mx["unit"] == "px",
          "ONE uncalibrated image drops the WHOLE run back to pixels")
    check(abs(mx["groups"]["N2 0J @ 0h"]["p50"] - px_med) < 1e-9,
          "the pixel fallback is identical to the no-scale run")
    check(any("no spatial calibration" in m for m in logs),
          "the fallback is logged, not silent")

    different = {("0", "a.tif"): 5.0, ("0", "b.tif"): 2.5}
    df = survival_size.build_size_payload(soft, per_image, logs.append,
                                          scale_by_key=different)
    check(df["unit"] == "um",
          "two different but valid scales still convert (per-image, as intended)")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_reading(tmp)
        test_units(tmp)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("all checks passed")
