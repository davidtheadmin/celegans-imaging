# Parked: scale normalisation for the staging pipeline

`normalize.py`, `test_normalize.py` and `canonical_scale.json` were written to
resample every image to one canonical µm/px before the staging model saw it, so
that apparent worm size would be constant regardless of acquisition
magnification.

**They were never wired in.** Nothing imported `normalize.py` — not
`vision/infer_stage.py`, not `vision/tiled_infer.py`, not `survival.py` — and
its own pre-flight checklist ("VERIFY BEFORE THE FIRST REAL TRAINING-PREP BATCH
RUN") was never completed either. So neither training nor inference ever passed
through it, and removing it changes no result. The model was trained, and runs,
on un-normalised images at whatever magnification each set was captured at.

Moved here rather than deleted for two reasons.

**The canonical value is still worth knowing.** 5.0954 µm/px was frozen from the
`Lowestmag_survival` capture set as the *coarsest* scale at which worms and eggs
can still be reliably counted — a floor to calibrate a new rig against, not a
conversion factor. It is a genuine setup recommendation and is likely to be
cited as one. What it is *not* is a measurement of any particular image, which
is why `launcher/survival_scale.py` reads each image's own TIFF tags instead.
Do not reintroduce this constant as a unit conversion.

**The code is a reasonable starting point** if scale normalisation is ever
actually wanted — most likely as part of retraining, where making the model
scale-consistent would matter more than it does for reporting. Two things in
`BACKLOG.md` were left unresolved and would have to be settled first: the
inference path must read through `_read_rgb` (`crop_wells._read` returns BGR, so
the RGB invariant only holds if both paths convert), and the first CLI run on
`Lowestmag_survival` must log exact passthrough at `scale = 1.0000` for all five
files — anything else means that batch was not all at one calibration and is not
safe as training data.

Parked 2026-08-18, when body size moved to micrometres read from per-image tags.
