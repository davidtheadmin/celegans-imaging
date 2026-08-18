# Bend-counting method calibration (2026-05-05)

This folder contains the calibration receipts for the head-angle bend-counting
algorithm used in the motility analysis pipeline.

> **⚠ The calibrated value is not the value that ships.** This calibration was
> performed at prominence **0.30 rad**. `launcher/motility_params.json` ships
> **0.50 rad**, and 0.50 was not in the sweep at all (it tested 0.10 / 0.20 /
> 0.30 / 0.40). The MAE of 1.8 bends/30 s quoted below therefore does **not**
> describe the shipped configuration, and no record survives of why the value
> was raised or what it was checked against.
>
> Nothing here says the shipped value is wrong — a higher prominence counts
> fewer, larger swings, which may well have been a deliberate improvement. But
> it is currently uncalibrated, and this is the only calibration record in the
> repository. **Resolve before publishing any bend rate:** either re-run the
> sweep including 0.50 and restate the MAE, or record the reasoning and the
> evidence for the change. Flagged 2026-08-18.

## Method choice

The production algorithm is **v1**: signed angle between the head vector
(skeleton point 5 to 0) and the body vector (skeleton point 30 to 20),
detrended, peaks counted with `scipy.signal.find_peaks`. Calibrated here at
prominence 0.30 rad; **the code ships 0.50** (see the warning above).

Validated against manual counts from a lab technician on 8 worms (4 fast WT,
4 slow). Mean absolute error: 1.8 bends/30s.

## What's in this folder

- `bend_calibration.py` - initial calibration script (multiple methods + prominence sweep)

**Missing from this folder** (referenced by the notes below but not committed):
`worm_diagnostic_all.py`, and the eight diagnostic PNGs `fast-WT-{1,3,4,10}.png`
and `slow-{1,2,3,4}.png`. The findings that follow were derived from them and
are kept as the record; the artefacts themselves were never archived.

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
*rate* is still correctly computed from valid frames **for worms classified as
colliding**, and for those the claim holds. It does **not** hold for worms
classified as *curled*: that path divides by every clean sub-track, including
sub-tracks too short to yield a signal at all, so a fragmented worm is
under-rated. Fragmentation is not uniform across conditions, so that part does
not cancel out between conditions. Around 1 in 8 worms in the calibration set
hit the skeleton-fit case.

## Re-running the calibration

Should imaging conditions change (different framerate, magnification, or worm
preparation), recalibrate by:

1. Get a lab technician to manually count bends on 5-10 worms across the motility spectrum
2. Update `MANUAL` list in `bend_calibration.py` with the new (path, worm_index, count) tuples
3. Run `bend_calibration.py`; sweep `head_angle_prominence` to find the value
   with lowest MAE. **Include the value the code currently ships in the sweep
   range.** Note the script hardcodes absolute paths in its `MANUAL` list and
   will not run as committed — point it at your own files first.
4. Update `motility_params.json` with the new prominence
5. Confirm the shape of the traces is sensible (`worm_diagnostic_all.py` is
   referenced here but is not in the repository; the per-video PNGs the pipeline
   writes to `per_video/` serve the same purpose)
6. Re-archive into `docs/calibration/` with new date
