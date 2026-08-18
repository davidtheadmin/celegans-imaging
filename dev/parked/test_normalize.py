"""Unit tests for the pure resampling contract in normalize.py.

Run from launcher/analysis with the launcher venv:
    python -m unittest test_normalize -v
"""
import unittest

import numpy as np

try:
    from normalize import resample_to_canonical, BelowFloorError
except ImportError:  # pragma: no cover - package import path
    from analysis.normalize import resample_to_canonical, BelowFloorError


def _feature_width(img, thresh=127):
    """Width in px of the bright vertical stripe on a middle row."""
    row = img[img.shape[0] // 2]
    if row.ndim == 2:  # RGB row -> collapse channels
        row = row.max(axis=1)
    return int((row > thresh).sum())


class ResampleToCanonicalTests(unittest.TestCase):
    def test_downscale_feature_length(self):
        # 400x400 RGB with a white vertical stripe 100 px wide (x=150..250).
        img = np.zeros((400, 400, 3), np.uint8)
        img[:, 150:250, :] = 255
        self.assertEqual(_feature_width(img), 100)

        # source finer than canonical -> scale 0.5 -> half size, half feature.
        out = resample_to_canonical(img, source_um_per_px=2.0,
                                    canonical_um_per_px=4.0)
        self.assertEqual(out.shape[:2], (200, 200))
        # feature length should track source_len * (source/canonical) = 100*0.5.
        self.assertAlmostEqual(_feature_width(out), 50, delta=2)

    def test_scale_one_is_exact_passthrough(self):
        rng = np.random.default_rng(0)
        img = rng.integers(0, 256, size=(64, 48, 3), dtype=np.uint8)
        out = resample_to_canonical(img, source_um_per_px=5.0954406,
                                    canonical_um_per_px=5.0954406)
        self.assertIs(out, img)                 # same object, no copy
        self.assertTrue(np.array_equal(out, img))

    def test_below_floor_raises_by_default(self):
        img = np.zeros((100, 100, 3), np.uint8)
        # source coarser than canonical -> scale 2.0 > 1 -> refuse.
        with self.assertRaises(BelowFloorError):
            resample_to_canonical(img, source_um_per_px=10.0,
                                  canonical_um_per_px=5.0)

    def test_below_floor_upscales_only_when_allowed(self):
        img = np.zeros((100, 100, 3), np.uint8)
        out = resample_to_canonical(img, source_um_per_px=10.0,
                                    canonical_um_per_px=5.0, allow_upscale=True)
        self.assertEqual(out.shape[:2], (200, 200))

    def test_nonpositive_scale_rejected(self):
        img = np.zeros((10, 10, 3), np.uint8)
        with self.assertRaises(ValueError):
            resample_to_canonical(img, 0.0, 5.0)
        with self.assertRaises(ValueError):
            resample_to_canonical(img, 5.0, -1.0)


if __name__ == "__main__":
    unittest.main()
