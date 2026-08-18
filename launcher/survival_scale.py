"""Physical scale for the Development readouts: micrometres per pixel.

WHERE THE NUMBER COMES FROM. Each image's own TIFF tags, written at capture
time by ``capture/app/capture_ops.py::save_still`` when a calibration was
active:

    ResolutionUnit (296) = 1     unit is "none"; the real unit is in the
                                 description, ImageJ's convention
    XResolution    (282)         pixels per micron, as a RATIONAL
    ImageDescription (270)       carries ``unit=um``

so ``um_per_px = 1 / XResolution``.

NOT from a fixed constant. ``launcher/analysis/canonical_scale.json`` froze one
value (5.0954 um/px) as the COARSEST magnification at which worms and eggs can
still be counted — a recommendation for setting the rig up, not a measurement of
any particular image. Using it as a conversion factor would silently mis-scale
every plate that was, correctly, imaged finer than the floor. The module that
consumed it (``analysis/normalize.py``) was never wired into inference or into
training-prep and is parked in ``dev/parked/``; see the README there.

WHY THIS LIVES IN THE LAUNCHER and not in ``vision/infer_stage.py``. Scale is a
property of the image file, not of the inference settings. Reading it here means
(a) the two-venv boundary contract is untouched, (b) the detection cache key
does not change — the same boxes are still the right boxes — and (c) detections
replayed from an earlier run's cache get scaled exactly like fresh ones. Putting
it in the CSV the vision side writes would have failed (c), which is most of the
rows in a combining run.

WHAT IS NOT MEASURED IS NOT ZERO. Every function here returns ``None`` for an
image with no calibration, and the caller is expected to say "not measured"
rather than substitute a default (invariant 7 in ARCHITECTURE.md).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# Generous plausibility window. This is here to catch a corrupt or
# wrongly-interpreted tag, not to police magnification: the rig's own range runs
# from roughly 15 um/px (whole 60 mm plate on the full sensor) to a few um/px in
# quadrant mode, and other builds may sit outside that.
_MIN_UM_PER_PX = 0.05
_MAX_UM_PER_PX = 200.0

_TAG_X_RESOLUTION = 282
_TAG_RESOLUTION_UNIT = 296
_TAG_IMAGE_DESCRIPTION = 270


def _rational(v) -> Optional[float]:
    """tifffile rationals arrive as (num, den); also accept a plain number.

    Same convention as ``analysis/counting._rational``, duplicated rather than
    imported so this module has no dependency on the counting pipeline.
    """
    if v is None:
        return None
    if isinstance(v, (tuple, list)) and len(v) == 2:
        num, den = v
        try:
            return float(num) / float(den) if float(den) else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def um_per_px(path: Path) -> Optional[float]:
    """Micrometres per pixel for one image, or None if it carries no scale.

    None covers every failure the same way on purpose — no tags, a non-TIFF, an
    unreadable file, a ResolutionUnit that is not the "none" convention we
    write, or a value outside the plausibility window. The caller reports the
    count of unscaled images; which flavour of missing it was is a log line, not
    a different return value.
    """
    p = Path(path)
    try:
        import tifffile
    except ImportError:                                   # pragma: no cover
        log.warning("tifffile unavailable — no image can be scaled")
        return None
    try:
        with tifffile.TiffFile(str(p)) as tf:
            tags = tf.pages[0].tags
            unit = tags.get(_TAG_RESOLUTION_UNIT)
            xres = tags.get(_TAG_X_RESOLUTION)
            desc = tags.get(_TAG_IMAGE_DESCRIPTION)
            unit_v = unit.value if unit is not None else None
            xres_v = _rational(xres.value) if xres is not None else None
            desc_v = str(desc.value) if desc is not None else ""
    except Exception as exc:                              # noqa: BLE001
        log.debug("no scale from %s: %s", p.name, exc)
        return None

    if xres_v is None or xres_v <= 0:
        return None
    # ResolutionUnit 1 = none, which is what save_still writes and what makes
    # the description's "unit=um" authoritative. 2 (inch) / 3 (cm) mean some
    # other tool wrote the file against a different convention, and guessing
    # which would be worse than reporting nothing.
    if unit_v is not None and int(unit_v) != 1:
        log.debug("%s: ResolutionUnit=%s, not the unit=um convention", p.name, unit_v)
        return None
    if "unit=um" not in desc_v.replace(" ", ""):
        log.debug("%s: no 'unit=um' in ImageDescription", p.name)
        return None

    val = 1.0 / xres_v
    if not (_MIN_UM_PER_PX <= val <= _MAX_UM_PER_PX):
        log.debug("%s: um/px %.4g outside the plausible window", p.name, val)
        return None
    return val


class ScaleReport:
    """Scales for a set of images, plus what to say about them.

    ``by_path`` maps every image to its um/px or to None. ``uniform`` is True
    when every image carries a scale, which is the only case in which the run
    may report micrometres — see ``survival_size.build_size_payload``.
    """

    def __init__(self, by_path: dict[Path, Optional[float]]):
        self.by_path = by_path
        vals = sorted(v for v in by_path.values() if v is not None)
        self.n_total = len(by_path)
        self.n_scaled = len(vals)
        self.n_missing = self.n_total - self.n_scaled
        self.uniform = bool(by_path) and self.n_missing == 0
        self.min = vals[0] if vals else None
        self.max = vals[-1] if vals else None
        self.median = vals[len(vals) // 2] if vals else None

    @property
    def spread_pct(self) -> Optional[float]:
        """Max/min as a percentage above 1, or None. A few tenths of a percent
        is rounding; a few percent means the working distance moved between
        plates and the size distributions are not strictly comparable."""
        if self.min is None or self.max is None or self.min <= 0:
            return None
        return 100.0 * (self.max / self.min - 1.0)

    def describe(self) -> str:
        if not self.n_scaled:
            return (f"no spatial calibration on any of {self.n_total} image(s) "
                    "— sizes stay in pixels")
        s = (f"{self.n_scaled}/{self.n_total} image(s) calibrated: "
             f"{self.min:.4g}–{self.max:.4g} um/px "
             f"(median {self.median:.4g})")
        sp = self.spread_pct
        if sp is not None and sp >= 1.0:
            s += f", a spread of {sp:.1f}% — the working distance moved"
        if self.n_missing:
            s += f"; {self.n_missing} image(s) carry no calibration"
        return s


def scan(paths: Iterable[Path]) -> ScaleReport:
    """Read the scale of every image once, and summarise."""
    by_path: dict[Path, Optional[float]] = {}
    for p in paths:
        p = Path(p)
        if p in by_path:
            continue
        by_path[p] = um_per_px(p)
    return ScaleReport(by_path)
