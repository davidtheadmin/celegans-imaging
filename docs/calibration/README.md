# Bend-counting method calibration (2026-05-05)

This folder contains the calibration receipts for the head-angle bend-counting
algorithm used in the motility analysis pipeline.

## Method choice

The production algorithm is **v1**: signed angle between the head vector
(skeleton point 5 to 0) and the body vector (skeleton point 30 to 20),
detrended, peaks counted with `scipy.signal.find_peaks` at prominence 0.30 rad.

Validated against manual counts from a lab technician on 8 worms (4 fast WT,
4 slow). Mean absolute error: 1.8 bends/30s.

## What's in this folder

- `bend_calibration.py` - initial calibration script (multiple methods + prominence sweep)
- `worm_diagnostic_all.py` - full-timeline per-worm diagnostic across 8 method variants
- `fast-WT-1.png`, `fast-WT-3.png`, `fast-WT-4.png`, `fast-WT-10.png` - fast WT diagnostics
- `slow-1.png`, `slow-2.png`, `slow-3.png`, `slow-4.png` - slow worm diagnostics

## Why v1 (and not others)

Eight method variants were tested:

- v1: head=5->0, body=30->20 (current production)
- v2: head=5->0, body=20->15 (shorter body reference)
- v3: head=5->0, body=15->10 (just-behind-head reference)
- v4: head=10->0, body=25->15
- v5: anterior tangent across segments 5..25
- v6: anterior tangent across segments 5..15
- v7: velocity-of-head reference, 0.5s window
- v8: velocity-of-head reference, 1.0s window

Key findings:

- v1, v2, v3 perform near-identically on slow worms (delta within +/-2 of manual)
- v5 and v6 (anterior tangent) overcount slow worms by ~2x (e.g. slow-1: manual 13, v5/v6 ~23). Unsafe for survival assays - would mask the dose-response at high toxicity.
- v7 and v8 (velocity reference) drop many frames on slow worms (52-79% valid only) and are unreliable.
- No method fixes worm fast-WT-4; that worm has only 48% valid skeleton frames in the source data, and all methods are computing rate correctly from the valid 48%. The undercount is upstream of bend counting (skeleton tracking issue), not an algorithm choice issue.

## Known limitation

For the small fraction of worms where Tierpsy fails to maintain skeleton fits
across more than ~30% of frames, absolute bend counts will be reduced. The
*rate* is still correctly computed from valid frames, and this affects all
conditions equally - between-condition comparisons remain valid. Around 1 in 8
worms in the calibration set hit this case.

## Re-running the calibration

Should imaging conditions change (different framerate, magnification, or worm
preparation), recalibrate by:

1. Get a lab technician to manually count bends on 5-10 worms across the motility spectrum
2. Update `MANUAL` list in `bend_calibration.py` with the new (path, worm_index, count) tuples
3. Run `bend_calibration.py`; sweep `head_angle_prominence` to find the value with lowest MAE
4. Update `motility_params.json` with the new prominence
5. Run `worm_diagnostic_all.py` to confirm shape of traces is sensible
6. Re-archive into `docs/calibration/` with new date
