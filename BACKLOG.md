# Polish backlog

Small items deferred from main work. Pick up when convenient.

> **Swept 2026-08-18.** Items verified as already shipped are marked
> **DONE** with the evidence rather than deleted, so the record survives.
> Several staging values quoted below were superseded by the 2026-08-05
> measurement — `launcher/vision/stage_conf.json` is authoritative for those,
> not this file.

## Storage (done 2026-05-28)

- `.trash` no longer leaks: reclamation deletes acked files directly (not move-to-trash), the recycle bin auto-purges after `CELEGANS_RETENTION_TRASH_MAX_AGE_DAYS`, and capture refuses on a full card with HTTP 507.

## UI

- **DONE (verified 2026-08-18).** ~~Soft-deleted thumbnails don't vanish from
  timeline.~~ Implemented: the tile fades and is removed on `transitionend`
  (`capture/app/static/app.js`). Original note:
- **Soft-deleted thumbnails don't vanish from timeline.** When a file is
  deleted in the timeline, the thumbnail is correctly marked "deleted" but
  remains visible. Should fade out / be removed from the strip after delete.

- **Motility assay nomenclature.** Subgroups within a motility session are
  currently labeled "plates" (inherited from the survival assay schema).
  Should be "videos" or "captures" — plates make no sense for motility.
  Check both the frontend labels and any session.json schema fields that
  surface to the user.

## Launcher mirror folder structure — MOSTLY DONE (verified 2026-08-18)

`launcher/sync.py` already mirrors to
`experiments/<experiment name>/<condition name>/<plate label>/<file>` using the
user-given names, with collision-safe suffixing, and `freecapture` is already
split into `pictures/` and `videos/`. Only two cosmetic items remain: the
literal folder name "Free captures", and dropping the top-level `experiments/`
segment. Original note:

Current mirror layout uses Pi-internal names that are fine for code but
unfriendly when browsing in Explorer:
Documents\WormScan
├── freecapture
└── sessions<session_id_timestamp>\plates<folder_name>\

Desired layout, mapped at the launcher (Pi stays as-is):
Documents\WormScan
├── Free captures
└── <user-given session name>
├── WT 0J
├── WT 10J
└── ...

Means:
- Rename `freecapture` → `Free captures` (or similar) at mirror time.
- Rename `sessions` top-level away (move user sessions to top level).
- Use the user-given session name (from session.json) instead of the
  generated session_id in the path.
- Use the condition name as the second-level folder (drop the
  intermediate `plates/` segment).

Note: the launcher's recovery logic relies on Pi-relative paths matching
the mirror structure. Renaming requires a stable mapping table held in
the launcher, OR the launcher writes a `.wormscan-meta` marker per
folder recording the original Pi path. Decide which when we pick this up.

## Launcher mirror: rename-orphan limitation

When an experiment is renamed in the browser, the existing mirror folder
is **not** moved. New files captured after the rename appear in a new
folder named after the new experiment name; files already mirrored stay
in the old folder. This is accepted as a minor papercut — the old folder
is harmless and can be deleted manually.

The root cause: the launcher has no stable mapping from session_id to
previous friendly folder names. A future fix would write a
`.wormscan-meta` marker per folder recording the original session_id,
allowing the launcher to detect renames and move the folder.

- On rename, offer to clean orphaned mirror folders from the launcher side (currently they stay).

## UI: delete sessions and conditions, not just plates — DONE (verified 2026-08-18)

Implemented: `DELETE /sessions/{id}` and `DELETE /sessions/{id}/conditions/{cid}`
in `capture/app/main.py`, soft-deleting to `.trash/` with confirmation dialogs,
exactly as specified below. Original note kept for the record:

Currently the timeline only supports deleting individual plates (and free
captures). Need bulk-delete operations:

- Delete entire session: removes all plates + session.json. Soft-delete
  to .trash/sessions/<sid>/ for recoverability.
- Delete a condition within a session: removes all plates assigned to
  that condition, but leaves the rest of the session intact.

Both need confirmation dialogs ("Delete session 'UV survival run 3' and
all 60 plates?"). Soft-delete semantics should match the existing
per-plate delete so retention can clean up later.

- [ ] Production motility plots (`make_video_summary_png`, `make_per_worm_trace_png`) draw straight lines across skeleton-failure gaps, which can be misleading. Insert NaN values into time/angle arrays at gap boundaries so matplotlib renders real visual gaps. Discovered during 2026-05-05 calibration revisit; was responsible for the apparent "step pattern" on worm 4 that turned out to be ~50% missing skeleton frames.
- [ ] Add `signal_coverage_pct` distinct from trajectory `coverage_pct` to motility CSV outputs. Current `coverage_pct` measures trajectory continuity (frames Tierpsy tracked the worm) but not head-angle signal validity (frames where skeleton was finite). Worm 4 reports 100% trajectory coverage but only ~48% signal coverage. Filter on `signal_coverage_pct >= X` would be more meaningful for analysis quality.

## Review (grid viewer)

- **Stream per-clip progress in the Review build dialog.** The build currently
  shows a single indeterminate "Building viewer…" spinner. The video generator
  prints one line per condition as it transcodes (the slow step); piping the
  child's stdout into the dialog and showing "clip N of M" would give real
  feedback on long first-run builds. Deferred from the initial Review feature.

## Crawling & analysis

- **Crawling under-count — Tierpsy segmentation fragmentation.** Worms visible
  all 180s are tracked by Tierpsy in only ~16–60s pieces. The 30s gate is the
  current pragmatic baseline (7 kept on 601 0J day-0 — note no denominator was
  recorded).

  **Re-assessed 2026-08-18. Two claims in the previous version of this note were
  wrong and are corrected here.**

  1. *"Post-processing levers are exhausted"* — true, but it does not mean what
     it was taken to mean. Our linker already allows 150 px over 5 s against
     Tierpsy's own 30 px over 0.83 s, so it is doing nearly all the joining work
     and has little left to give. More importantly it runs **after** SKE_FILT, so
     rejoining fragments recovers span and coverage (which is why the worm then
     passes the gate) but recovers **no features**. Only joining earlier, inside
     Tierpsy, helps.
  2. *"Sweep segmentation params — the only root-cause lever left, deferred"* —
     that sweep **ran and won**. `dev/tools/tierpsy_param_sweep.py` records Phase
     3c: `worm_bw_thresh_factor` 1.0 → 0.95 cut fragments **45 → 16**, and 0.92
     is committed in `crawling_params.json` along with `thresh_C=5` and
     `thresh_block_size=31`.

  **Still open, and directly indicted by our own diagnostic:** `mask_min_area` is
  still 500 while the sweep harness records *"36 of 40 mid-video trajectory
  breaks happen with the worm's measured area in [500, 600]; p1 = 504, median =
  612"* — the floor sits inside the signal. And **`traj_min_area` = 500 is a
  second, independent floor at TRAJ_CREATE that has never been in any sweep
  grid**, so lowering `mask_min_area` alone will have the same cut re-imposed.
  Sweep them together.

  Also open: `traj_max_frames_gap` = 25 is **0.83 s at 30 fps**, against the ~1 s
  dropout it exists to bridge. It must be swept jointly with
  `traj_max_allowed_dist`, because a longer gap needs a longer allowed distance
  (the worm keeps moving while unseen).

- **"Raising `traj_max_allowed_dist` to 175 REGRESSED (119 fragments)" — treat as
  untrusted, not as a closed finding.** Three reasons: it was measured in the
  same session in which the cache bug was discovered, and nothing records that
  Clear cache was ticked; 30 → 175 is a 5.8× jump with 40–70 unexplored; and
  mechanically it raised link distance without raising `traj_max_frames_gap`,
  which loosens same-frame association (more ID swaps → more fragments, which is
  what 119 shows) without helping gap-crossing at all. Re-run it properly before
  believing it.

- **Check the sweep harness timeout before trusting any sweep result.**
  `tierpsy_param_sweep.py` takes its timeout from `analysis_video_timeout_s`
  (600 s) while the production crawling pipeline needed 3600 s. On timeout the
  harness writes NaN metrics and the summary plot renders a missing point — a
  silent gap indistinguishable from a bad parameter value. This biases the
  `mask_min_area` arm specifically: lower floors admit more blobs and run
  slower, so the runs most likely to help are the most likely to time out. The
  `error` column in `comparison.csv` records what actually happened.

- **Before any of the above, read the `per_worm` sheet you already have.** It
  retains every worm with `track_duration_s`, `skeleton_coverage` and
  `passed_filter`. One cross-tab of span ≥ 30 against coverage ≥ 0.70 says which
  half of the gate is binding, at zero compute cost, and reorders everything
  else here. Note `SKELETON_COVERAGE_MIN = 0.70` is a module constant with no UI
  and no config field, so unlike `min_span_s` it cannot be re-tuned at
  aggregation.

- **The crawling log line is not evidence.** `_linker_log` hardcodes
  `worms_dropped: {"total": 0, "by_reason": {}}`, so every run prints
  `dropped_total=0 by_reason={}` regardless of what happened, and it omits
  `ambiguity_skips` (which *is* written to `per_video/*_analysis_log.json`). The
  note that once read "the engine drops almost nothing (5 too_short, 0
  debris/flicker)" described the **motility** engine, not this one. Fix the log
  line; it has already misled once.

- **DONE 2026-08-18: analysis cache now invalidates on param change** — and on
  pipeline change, which was an unrecorded second failure mode: motility and
  crawling share `_wormscan_cache/<stem>/`, so running one then the other on the
  same folder made the second silently reuse the first's tracking, with the
  answer depending on which order you ran them in. A stamp file now records both
  the parameter fingerprint and the pipeline; anything unstamped or mismatched is
  re-run. Note the parameter files are still read once at agent construction, so
  editing them still requires a launcher restart. Original note:

- **Analysis cache doesn't invalidate on param change.** `_wormscan_cache` hits
  on the existence of `Results/<stem>_featuresN.hdf5`, ignoring whether the
  cached result was produced with the current params. Changing
  `crawling_params`/`motility_params` and re-running silently serves the stale
  result. Bit us this session (dist=175 output cached under dist=30 params).
  Fix: include a hash of the effective Tierpsy params in the cache key (or write
  a params fingerprint next to the cache and invalidate on mismatch).

- **Validate the 30s crawling gate across UV doses.** The 30s min-track gate was
  validated on ONE video (601 0J, day-0). Higher-dose worms (10J) move less /
  may fragment differently. When running the full batch, confirm 30s stays
  sensible per-dose; may warrant per-condition review. The gate re-applies at
  aggregation (no re-run needed to re-tune).

- **Docker Desktop CPU allocation limits parallel speedup.** The parallel
  pipelines autosize workers from `docker info` NCPU/MemTotal. Docker Desktop's
  Linux VM caps CPUs below the host (capped at 4 on the dev box → auto picks 2 →
  1.76×). Before the full UV batch on the 8C/16GB analysis machine, raise Docker
  Desktop → Settings → Resources CPU/RAM so auto scales to ~4 workers. Settings
  change, not code.

## YOLO staging — scale normalization (`launcher/analysis/normalize.py`)

New module committed untracked (nothing imports it yet). VERIFY BEFORE THE FIRST
REAL TRAINING-PREP BATCH RUN — these generate images we annotate, so a silent
error here poisons the training set:

- [x] **1(a) Channel conversion is clean.** `_read_rgb` uses
  `cv2.cvtColor(img, cv2.COLOR_BGR2RGB)` (not axis slicing). Verified 2026-07-10.
- [ ] **1(b) Inference must read through `_read_rgb`.** `crop_wells._read`
  returns BGR; the RGB invariant only holds if inference also converts. A loud
  banner comment was added above `_read_rgb`. **Re-confirm when wiring inference
  in**: the inference path must call `_read_rgb` (or apply the same BGR→RGB),
  never a bare `crop_wells._read`. Silent failure = channels swap, no error, mAP
  degrades.
- [x] **2. `warn` must not upscale.** Fixed 2026-07-10: `--on-below-floor warn`
  now logs + skips + writes NO output (same as `refuse`; only log wording
  differs). Below-floor files always raise `BelowFloorError` and are skipped.
  `allow_upscale=True` on `resample_to_canonical` is a deliberate programmatic
  escape hatch only — never reachable through the CLI/`warn`. Verified: warn run
  on a synthetic below-floor TIFF wrote zero image files.
- [ ] **3. Passthrough sanity on first run.** When the CLI is first run on the
  `Lowestmag_survival` batch, ALL FIVE files must log EXACT passthrough,
  `scale = 1.0000`, zero interpolation. If any file logs a non-exact scale (e.g.
  `0.9998`), the batch is NOT all at one identical calibration as assumed —
  STOP and report the spread rather than writing it as training data.

## Staging model — per-class confidence thresholds

**Plumbing done 2026-07-27; calibration still open.**

Per-class thresholds now exist end to end. `launcher/vision/stage_conf.json` is
the single source of truth: `infer_stage.py` reads it whenever no threshold flag
overrides it, and the launcher reads the same file to seed the seven per-stage
sliders in the Development card (and to power their *Reset to defaults*). The
*Analyze on laptop* button inherits it by passing no `--conf` at all, so the
button and the batch pipeline cannot silently disagree. Values are applied to
every raw tile detection **before** any NMS, and persisted as
`survival_class_conf`.

**UPDATE 2026-08-18: the numbers below are superseded.** The thresholds were
re-measured on 2026-08-05 from 7,265 detections and are no longer the values
quoted here; `stage_conf.json` records what they are and how they were derived.
The count-vs-threshold curve was found to have **no flat region**, so the
"set it where the curve flattens" instruction below cannot be followed — that
was measured and refuted, and the resolution taken was a uniform floor with one
data-driven exception. What remains genuinely open is the **cross-check against
hand-labelled animals**, which has not been done.

**What is still open — the actual calibration.** The shipped numbers
(`_default` 0.25; egg/L1/L2/L3/young adult 0.30; adult 0.35) were **chosen, not
measured**. They lift the classes the model is known to be shakiest on off the
old uniform 0.25 and leave the rest near it; that is a starting point, nothing
more. Nothing downstream distinguishes a tuned threshold from a guessed one, so
they will look exactly as authoritative in `run_info` as calibrated ones would.

To close this out:

- [ ] Run `dev/tools/stage_conf_report.py` (VISION venv) on a real plate set. It
      reports, per class, the count surviving each threshold from 0.05 to 0.60;
      box-size percentiles; and the seam-fragment / cross-class-duplicate rate.
- [ ] Set each class's threshold where its count-vs-threshold curve flattens —
      the steep part is noise dying, the flat part is real worms. **A class with
      no flat region is a retrain problem, not a threshold one.** Say so in the
      report rather than picking a number.
- [ ] Cross-check against manual stage counts on at least one plate per dose,
      especially across the L2/L3 cutoff, before any count is reported as data.
- [ ] Raising a threshold drops uncertain calls from BOTH sides of the survival
      ratio — it does not reassign them. Confirm N per plate stays high enough
      that the remaining ratio is still meaningful.

Until that is done the "Analyze on laptop" annotated counts (CURRENT_STATE §2.6 /
§6.18 of the archived docs/history/CURRENT_STATE.md) and the Development
workbook are eyeballing aids only.

## Staging — duplicate boxes on one worm (fixed 2026-07-27, wants field checking)

**Symptom:** one worm got two boxes — a correct one plus a smaller box covering
only part of it, usually a different stage.

**Cause, three layers deep.** (1) The merge in `tiled_infer.py` was per-class NMS
only, so the same worm labelled L2 in one tile and L3 in another was never even
compared — and that pair biases survival % in both directions at once, since L2
is a non-survivor and L3 a survivor. (2) NMS is IoU-based, and IoU is structurally
the wrong test for a nested box: a stub a third the area of the correct box has
IoU ≤ 0.33 and survives a 0.45 threshold even when it is entirely inside.
(3) The fragment existed at all because the overlap band was too narrow — at
overlap 0.2 the frame is 8×7 = 56 tiles stepping 541×486, sharing only 135×122 px,
so any worm whose box exceeds that is **not** guaranteed to sit fully inside any
tile and gets sliced by every seam that touches it.

**Fix.** Seam-touching detections are flagged `truncated` instead of dropped, and
a truncated box is removed only when ≥ `cover_frac` of its own area sits inside a
higher-scoring box of any class (`covered_fraction`, not IoU). A fragment nothing
covers survives, so a worm is never lost. Overlap default raised 0.2 → 0.35
(72 tiles, ~29% slower, guarantee 237×213 px) so fewer fragments are generated in
the first place. All three are settings in `stage_conf.json`.

**Field result 2026-07-27: "barely any extra boxes anymore."** Three residual
cases came back; the second is now fixed, the third is below as its own item.

**Fixed in the same pass — one worm, two labels, near-identical boxes** (reported
as "a regular worm and one smaller worm got annotated as L3 and L4 with a very
similar sized box"). Per-class NMS structurally cannot see this: it only ever
compares boxes of the same class. Seam suppression does not fire either, because
both boxes sit in a tile interior and are never flagged truncated. Fix:
`merge.class_agnostic_iou` is now **on at 0.70** — one extra NMS across all
classes. At 0.70 the two boxes must be essentially the same rectangle to merge,
which one object produces and two neighbouring worms realistically do not
(verified against synthetic side-by-side and crossing pairs). Lower to ~0.55 if
pairs persist; raise if adjacent worms start being collapsed.

Still to do:

- [ ] Confirm on real plates that the duplicate is actually gone and nothing real
      disappeared — compare a run at `--no-seam-suppress` against the default.
      Each pass now has its own kill switch (`--no-seam-suppress`,
      `--no-class-agnostic`, `--no-size-gate`) so a regression can be bisected
      without editing the config.
- [ ] Check the box-size percentiles from `stage_conf_report.py`: if w_p95/h_p95
      still exceed 237×213 px, worms are being sliced even at overlap 0.35 and it
      should go higher.
- [ ] The general containment rule (suppress ANY mostly-contained box, not just
      seam-flagged ones) is deliberately NOT enabled — it would catch more
      duplicates but can eat a real L1 or egg lying on top of a coiled adult.
      Revisit only if the targeted rule proves insufficient.

## Staging — egg counting is now a toggle (done 2026-07-27)

"You rarely want to know how many worms of what are there AND eggs at the same
time. The only time you want egg counts is an egg survival, or when you put a
drop of eggs from bleaching."

`exclude_classes` in `stage_conf.json` ships as `["egg"]`. A **"Count eggs"**
checkbox appears in the launcher's Development card (persisted as
`survival_count_eggs`) and beside the capture UI's *Analyze on laptop* button;
the web-UI flag rides to the laptop as an `X-Count-Eggs` response header on
`/analyze/next`, with the Pi acting purely as a relay so future options need no
Pi deploy.

Two decisions worth remembering:

- Exclusion happens **pre-NMS**, not as an output filter. An egg box that is
  never created also cannot suppress a real L1 it overlaps — verified
  synthetically, and directly relevant to the L1/L2 problem above.
- An excluded class is reported as **"not counted"**, never `0`. `counts.txt`
  says so, the Excel drops the column rather than zero-filling it, and `run_info`
  names the exclusion. `0` would read as "no eggs on this plate" for a plate that
  might be covered in them.

Survival percentage is unaffected either way — eggs were already outside the
denominator in `SURVIVAL_CONFIG`.

- [ ] Needs a **Pi deploy** (`scripts/deploy.sh`) for the web-UI half:
      `capture/app/routers/analyze.py`, `static/index.html`, `static/app.js`. The
      launcher half works immediately. Until the Pi is deployed the button keeps
      working and falls back to the `stage_conf.json` default (eggs off).

## Staging — debris scoring HIGH on 'adult' (gate built, needs measuring)

Reported 2026-07-27: "quite some debris which it detects as adult with quite a
high confidence. That's weird because they look totally different. They are small
little globs."

**Why no confidence threshold can fix this.** The model is *confident*, so raising
`class_conf["adult"]` does not touch these — it only deletes real adults. The
usable signal is size: staging is fundamentally a size readout, the stages are
ordered egg < L1 < L2 < L3 < L4 < young adult < adult, and a small glob labelled
"adult" is not an uncertain adult, it is not a worm at all. That is a
*plausibility* failure, not a *confidence* failure, and it needs a different lever.

**Built:** `class_size_px` in `stage_conf.json` — per-class `[min, max]` on
`sqrt(w × h)` in full-frame px, applied to raw detections before any NMS.
Seam-truncated boxes are exempt (a clipped worm is legitimately undersized and
gating it would delete a real worm).

**UPDATE 2026-08-18: no longer empty.** `class_size_px` was measured and
populated on 2026-08-05 and now gates five classes. **But the `adult` lower
bound is still wrong for the purpose**: it sits well below the median size of an
L4, so the small debris this gate was built to catch still passes. The block's
own README explains why that happened — percentiles cut from the model's own
detections let the debris set its own lower bound — and the instruction below to
set the adult floor by hand from L4's median has not been carried out. Original
note:

**Shipped empty, i.e. off.** The pixel size of a stage depends on the
magnification, so there is no honest default; a guessed bound silently deletes
real worms, and a deleted worm does not announce itself in the counts.

- [ ] Measure it. `launcher\vision\.venv-vision\Scripts\python.exe
      dev\tools\stage_conf_report.py --suggest "<folder of plates>"` writes
      `stage_conf_suggested.json` (paste-ready) plus, per class, the fraction each
      bound would remove.
- [ ] **Read the removal counts before pasting.** The percentiles are cut from the
      model's own detections, so a class already polluted with debris has its lower
      bound set BY that debris and the gate will not remove it. For adult
      specifically — the contaminated class — set the lower bound by hand instead:
      an adult cannot be smaller than a typical L4, so L4's median is the natural
      floor.
- [ ] Check the report's ordering line. If median size does NOT rise along
      egg → adult, the model is not separating those stages by size and they must
      not be gated — that is a retrain problem and the report says so.
- [ ] Longer term this is a training-data problem: add hard-negative debris crops
      to the next staging model rather than filtering them out downstream forever.

## Staging — stage calls skew OLD on mixed plates (OPEN, discuss before building)

Reported 2026-07-27: "on mixed plates it tends to call worms older than they are.
It basically never calls anything L1 and L2, while when I let it analyze a
survival it's actually pretty good at that — but there it's only L1 and L2 on a
plate."

### What the model file says

`staging.pt`'s own `train_args` (readable without torch: the `.pt` is a zip, and
a permissive `Unpickler` walks the checkpoint pickle):

| setting | value | why it matters |
|---|---|---|
| `scale` | **0.0** | no random rescaling — absolute worm size WAS preserved in training |
| `multi_scale` | 0.0 | same |
| `mosaic` | **0.0** | every training sample is a verbatim crop of ONE plate |
| `degrees`/`translate`/`shear`/`perspective` | 0.0 | no spatial augmentation at all |
| `mixup`/`cutmix`/`copy_paste`/`erasing` | 0.0 | no compositing augmentation |
| `fliplr`/`flipud` | 0.5 | the only geometric augmentation |
| base model | `yolo11m.pt` | (docs previously said 11n — corrected) |
| val | P 0.752, R 0.754, mAP50 0.789, mAP50-95 0.489 | pooled; no per-class breakdown in the ckpt |

`scale: 0.0` is good news and it **rules out the obvious explanation**: the model
was not trained to be scale-invariant, so it can read the absolute size the whole
tiling scheme is built around.

### Leading hypothesis: a training shortcut, not a size-reading failure

With `mosaic: 0.0` and no spatial augmentation, each training image is one real
plate crop. If those plates were **synchronised** — one stage per plate — then
every training tile contained **exactly one class**. Under that condition the
detector never has to discriminate stage *within* an image: any image-level cue
(lawn texture, worm density, illumination) predicts the label perfectly, and
gradient descent takes the cheapest route available. The model can reach
P/R ≈ 0.75 having learned *"what stage is this plate?"* rather than *"what stage
is this worm?"*.

That one mechanism predicts both halves of the report exactly:

- uniform survival plate (only L1/L2) → the image-level cue is correct → good calls;
- mixed plate → one guess applied to every worm in the tile, skewed toward
  whatever dominates the tile's appearance → everything reads old, and L1/L2
  essentially never win.

**The current validation cannot detect this.** A random split of the same uniform
plates lets the shortcut work at val time too, which is exactly how P/R ≈ 0.75
coexists with the observed failure. A val set that cannot fail is not measuring
the thing that matters.

### Direct evidence from the trainset folders (2026-07-27)

`Documents\WormScan\experiments\` makes the shortcut hypothesis concrete —
almost every training set is **one stage per folder**:

```
Train_L1_staged      Trainset_L1        Trainset_L1_more   Trainset_L1_moremore
Train_eggs bleached  Train_gravidAdult  Train_youngAdult_more
Trainset_Eggs+Adult  Train_survival     Train_mix   <-- the only mixed one
```

That is exactly the condition under which "what stage is this plate?" is a
sufficient hypothesis for the training loss. Adding another single-stage set
(e.g. `Train_gravidAdult`) is still useful for appearance coverage, but on its
own it **reinforces** the shortcut rather than removing it. Weight future
annotation effort toward mixed plates, and put mixed plates in val.

### Ladder — cheapest first

- [ ] **Rule out the mundane cause first.** If the mixed plates were imaged at a
      different FOV/working distance, every worm is bigger in pixels and every
      stage shifts older, with no model pathology at all. Compare the size
      percentiles from `stage_conf_report.py` between a mixed set and a survival
      set for the same nominal stage. Cheap, and embarrassing to miss.
- [ ] **Check whether L1/L2 fire at all below our own floors.** We set L1 = L2 =
      0.30 but L4 = 0.25, so if young calls come back weaker we are compounding
      the bias ourselves. Section 1 of the report answers this directly. A
      band-aid, but a legitimate one.
- [ ] **Check what the cross-class NMS is eating.** Run a mixed plate with
      `--no-class-agnostic`. If L1/L2 boxes reappear as duplicates under older
      labels, the model *does* fire young labels and merely ranks them lower —
      a calibration problem, addressable with per-class score offsets. If they do
      not appear at all, the model genuinely is not seeing them, and only
      retraining helps. **This single test splits the whole problem in two.**
- [ ] **The decisive experiment: a context-sensitivity test.** Build a tile-sized
      canvas of sampled plate background, paste ONE detected worm into it **at
      the original pixel scale** (so absolute size is untouched), and re-run.
      Compare the isolated call against the in-context call across many worms. If
      small worms systematically flip from L4 to L1 once their neighbours are
      removed, the shortcut hypothesis is confirmed outright.
- [ ] **The real fix: retrain with `mosaic=1.0`, keeping `scale=0.0`.** Mosaic
      stitches four training images into one canvas, so worms from four different
      plates — and therefore different stages — co-occur in a single training
      image, and image-level cues stop being predictive. With `scale=0.0` the
      mosaic pieces are not resized, so this destroys the shortcut **without**
      touching the size signal. `close_mosaic: 10` is already set. One line in
      the training command, with a real mechanism behind it.
- [ ] **Put mixed plates in the VALIDATION set**, not just the training set.
      Without that there is no way to tell whether any of the above worked.
- [ ] Consider adding rotation (`degrees`) — worms lie at arbitrary angles and
      flips only cover four orientations. Secondary, but nearly free.

### Incidental: the egg toggle may already help L1

Eggs and L1 are the two smallest, most confusable classes. A synthetic check
confirmed an egg detection outscoring and swallowing an overlapping L1 under the
0.70 cross-class NMS. Since eggs are now excluded **pre-NMS** by default, that L1
survives. Watch whether L1 counts on mixed plates move at all after this change,
before attributing everything to the model.

## Staging — nested same-class boxes on big worms (fixed 2026-07-27)

Found on `Train_gravidAdult` while pre-annotating for Roboflow: duplicates were
back. Measured across all 15 frames rather than guessed:

| check | result |
|---|---|
| same-class pairs above the 0.45 per-class NMS threshold | 1 of 416 — NMS working |
| cross-class pairs above the 0.70 class-agnostic threshold | 0 — working |
| **`adult` nested >=90% inside another `adult`** | **19** |
| `egg` / `L2` nested inside `adult` | 3 — real biology, must survive |
| adult box size sqrt(w·h) | p50 134, max 204 px vs a 237 px guarantee |

So this was NOT a slicing problem (overlap 0.35 is ample; nothing exceeds the
guarantee) and NOT a config mistake. The inner boxes are 50–71% the linear size
of the outer, which puts their IoU at **0.25–0.45** — every one of them sits just
under the NMS threshold, and that is structural: a box ~60% the linear size of
another has IoU ~0.35 by construction. No NMS threshold can reach these without
also merging genuinely distinct worms. Only 11/19 are near a seam, so seam
suppression cannot cover it either.

Why it never appeared before: survival plates are L1/L2 — small and separated.
Gravid adults are large, coiled and clumped, so the model emits a partial box
*and* a whole box on one worm.

**Fix: `merge.same_class_cover_frac` (0.8, on).** Drop a box when that fraction
of its own area sits inside a LARGER box of the **same class**. Same-class only,
and that restriction is the whole safety argument — two worms at the same stage
are the same size, so one cannot be nested inside the other, whereas cross-class
nesting is real (a gravid adult is full of eggs). The larger box wins regardless
of score: a partial detection sometimes outscores the whole worm, and keeping
the confident fragment would be backwards.

Replayed over the existing 15 frames: **615 -> 593 boxes, and every one of the 22
removed is an `adult`** — no egg, L1, L2 or L3 box is touched at any threshold
from 0.7 to 0.9. Disable with `--no-same-class-nesting`.

This also supersedes the earlier "general containment rule" note above: the
blanket version was correctly refused, because at any useful threshold it would
have deleted the eggs inside the gravid adults. Restricting to same-class is
what makes containment safe.

## Staging — extra box where two worms sit close together (OPEN, not fixed)

Reported 2026-07-27: "when there are 2 or more worms close together it sometimes
gets confused and puts an extra box."

**Deliberately not fixed, pending an example image.** Neither existing pass
covers it: the spurious box is not seam-flagged when both worms are in a tile
interior, and it does not reach IoU 0.70 against either real worm, so
class-agnostic NMS leaves it alone. Every rule that would catch it is dangerous
in exactly this situation:

- suppressing a box that *contains* two or more other kept boxes assumes the
  containing box is the false one — but when the model splits one coiled worm
  into two, the containing box is the correct one and the two inner boxes are the
  error, and nothing downstream can tell those cases apart;
- suppressing any mostly-contained box regardless of seam origin (the "general
  containment rule" above) eats a real L1 or egg lying across a larger worm.

Both trade a cosmetic duplicate for a silently lost worm. Since the counts are
the output, a lost worm is strictly worse than an extra box.

- [ ] Get a preview PNG of the failure (tick "Save preview PNGs" in the Worm
      Survival card, or run with `--preview-dir`) showing the extra box and the
      two worms, then pick a rule that fits what the box actually is.
- [ ] `stage_conf_report.py` section 3 already counts cross-class overlapping
      pairs; extend it to report *how many* kept boxes each box contains, which
      quantifies how often this happens before any rule is chosen.
