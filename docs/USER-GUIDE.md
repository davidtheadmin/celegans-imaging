# WormScan user guide

How to run an experiment end to end, and — the part that matters most — **what
the numbers mean and which of them you can trust**.

Installing is a separate document: [`launcher/INSTALL.md`](../launcher/INSTALL.md).
This one assumes WormScan is installed and its status dot is green.

---

## The loop, in one picture

```
   Pi web UI                 WormScan launcher              Your folder
 ┌───────────────┐         ┌───────────────────┐        ┌────────────────┐
 │ make a session│         │  syncs by itself  │        │ Documents\     │
 │ add conditions│  ─────> │  every 2 minutes  │ ─────> │   WormScan\    │
 │ add plates    │         │                   │        │                │
 │ capture       │         │  Open Analysis    │ ─────> │  _analysis_... │
 └───────────────┘         └───────────────────┘        └────────────────┘
```

You do the imaging in a browser talking to the Pi. The launcher copies
everything to your computer on its own. Then you point an analysis at the
folder it created.

---

## 1. Imaging (the Pi's web UI)

Click **Open Imaging UI** in the launcher. That opens the Pi's page in your
browser, already authenticated — you never type the token into a browser.

### Set up before you capture

**A session** is one experiment. Create it and it gets a folder named from the
date and a short hash.

**Conditions** are your experimental groups. Name them
`<strain> <dose><unit>` — for example `N2 500J` or `N2 250uM`. This is not
cosmetic: the analysis pipelines parse that pattern to build dose-response
curves, and a condition that does not match it lands in a visible
`__unparsed__` group instead of being silently dropped.

**Plates** go inside conditions, numbered. On disk each becomes
`<condition>_<name>_plateNN`.

**Assay mode** decides what a capture produces:

| Mode | Produces | For |
|---|---|---|
| `motility` | 30 s H.264 video, 2028×1520 @ 30 fps | Motility, Crawling |
| `survival` | full-resolution TIFF still, 4056×3040 | Development, Colony Survival |

### Before the first capture of a session

- **Focus.** The page shows a live preview and a focus score (higher is
  sharper). Adjust until it stops improving.
- **Exposure.** Lock auto-exposure once the plate looks right, so every plate
  in the session is exposed the same. EV bias defaults to −1.0 and clamps at ±3.
- **Spatial calibration** (optional but worth it). Enter the field-of-view width
  in cm and the Pi embeds ImageJ-readable µm-per-pixel tags in every TIFF, so
  stills open pre-scaled in microns. **This scales the image file only — it does
  not make the motility or crawling numbers physical.** See
  [Reading the output](#reading-the-output).

### Capturing

Capture per plate from the session page. **Free capture** takes a one-off still
or video outside any session, which lands in `pictures/` or `videos/` by date —
useful for test shots, not for experiments.

**Analyze on laptop** grabs a full-resolution frame and runs the staging model
on it immediately, opening an annotated image and a count. It is a **live
sanity check while you are at the microscope, not a measurement.** See the
caveats.

If the Pi is low on disk it refuses to capture rather than filling up (HTTP
507). A retention daemon deletes the oldest already-synced files to stay above
5 GB free, so **anything not yet synced to a laptop is never deleted** — but
don't leave a session unsynced for weeks.

---

## 2. Syncing

Nothing to do. The launcher polls the Pi every two minutes, downloads anything
new, checks a SHA-256 for each file, and only then tells the Pi it can consider
that file safely delivered. A partial download can never be mistaken for a
complete one.

Files land under your mirror folder (`Documents\WormScan` by default):

```
experiments\<session>\<condition>\<plate NN>\   <- your experiment data
pictures\<date>\                                <- free-capture stills
videos\<date>\                                  <- free-capture videos
```

**Sync now** forces a check. The dot goes green after a clean pass. If it goes
red, the message says which problem it is — see the table in `INSTALL.md`.

---

## 3. Analysis

**Open Analysis**, pick a mode, point it at a folder, press Start. The folder
should be the level *above* your condition folders — usually
`experiments\<session>\`. Discovery walks three levels down and skips anything
beginning with `_` or `.`, so previous output folders are never re-analysed.

Every mode writes a **new timestamped folder** and never overwrites a previous
run, so you can never lose an earlier result by re-running.

**One caveat, for Motility and Crawling only.** Tierpsy's tracking is cached per
video so a re-run does not repeat the slow step. The cache records which
parameters and which pipeline produced it, and is ignored automatically when
either has changed — but if you edit `motility_params.json` or
`crawling_params.json` while the launcher is open, **restart the launcher**: the
parameter file is read once at startup.

### Motility — swimming, from video

Needs a container engine (see `INSTALL.md` step 3). Videos go through Tierpsy
for tracking and skeletonisation, then WormScan's own bend counting.

**Options:** minimum fragment length (default 5 s), clear cache, and optional
video renders (tracked, curvature, side-by-side, per-worm traces).

Note the minimum-fragment dial only bites above 10 s: worms observed for less
than 10 s are always dropped by a fixed internal gate, so any setting at or
below 10 s gives the same result.

**Output** in `_analysis_<timestamp>\`:

| File | What it is |
|---|---|
| `motility_results.xlsx` | one sheet per condition, plus `_summary` |
| `motility_summary.csv` | the summary sheet, for scripting |
| `overview.png` | all conditions at a glance |
| `per_video\*.png` + logs | one plot and one log per video |

The headline number is **BPM** — body bends per minute.

### Crawling — locomotion on a plate, from video

Also needs the engine. Same videos, **different Tierpsy parameters** — tuned
for larger, slower-moving objects — and a different track-linking method. The
two pipelines are deliberately not interchangeable.

**Options:** minimum track span (default 30 s), renders.

**There is a second filter you cannot see.** A worm must *also* carry a skeleton
on at least 70% of the frames it was tracked in. That threshold is fixed in the
code and is not on the dialog, so a worm visible for the whole video can still
be excluded on skeleton quality alone. If your worm counts look low, this is the
first thing to check — `per_worm` keeps every worm with a `passed_filter` column
and both inputs, so you can see which of the two rejected it.

**Output** in `_crawling_analysis_<timestamp>\`: `crawling_results.xlsx`
(`per_worm` and `per_condition` sheets), `crawling_summary.csv`,
`overview.png`, optional renders.

Gives speed, reversals, path tortuosity, activity fractions, and body-length-
normalised versions of the distance metrics.

Three things to know before reading those numbers:

- **`overview.png` plots only the pixel-based columns.** The body-length-
  normalised ones — the columns that exist precisely to cancel out
  magnification — are in the spreadsheet only. Worm length per condition drifts
  substantially between imaging days, so **do not compare conditions across days
  from the overview figure**; use the `_bls` columns.
- **Path length and tortuosity are biased by tracking quality.** Path length
  sums only consecutive tracked frames, while net displacement spans the whole
  track including gaps. A badly tracked worm therefore reports a shorter path
  and a lower tortuosity than it should, and tracking quality can correlate with
  treatment.
- **Worms that Tierpsy tracked but never measured are reported blank, not
  zero.** They still appear in `per_worm` and still count toward
  `n_worms_total`, but their speed, reversal and bend columns are empty and they
  are excluded from the condition means.

### Colony Survival — clonogenic assay, from stills

**No container engine needed.** Pure image analysis of crystal-violet stained
single wells: finds the well circle, flattens illumination, thresholds, splits
touching colonies with a watershed, filters by real colony diameter.

**Options:** split sensitivity (default 3.0), minimum colony size in µm
(default 200), detection sensitivity, colony smoothing, and a **same threshold
for every plate** tickbox that reveals an absolute stain threshold.

That last one is meant for dose series, where a per-plate automatic threshold
can make plates incomparable. Be aware it is not a perfect fix: illumination
flattening still runs per plate, so a plate whose colonies have merged into
large sheets is treated differently from a sparse one.

**Output** in `_counting_analysis_<timestamp>\`: `counting_results.xlsx`
(`per_colony`, `per_plate`, `per_condition`), `counting_summary.csv`,
`overlays\*.png`, `log.txt`.

**Always look at the overlays.** They show exactly what was counted as a
colony. It is the only way to catch a systematically wrong threshold.

### Development — developmental staging, from stills

**No container engine needed** (it uses the bundled model, not Tierpsy). Each
image is tiled, every tile goes through a YOLO model that classifies worms by
stage, and the boxes are merged back together.

**The headline readout is the mean stage index** — where each animal sits on the
L1 → adult scale — together with stage composition and body size.

**A survival percentage is still computed and written to the workbook, but it is
deliberately absent from every figure.** In a full dose experiment the
denominator collapses at high dose: one strain lost most of its animals, so the
percentage was computed over a handful of survivors and *rose* with dose — an
inverted dose response that was an artefact of the shrinking denominator, not
biology. The definition, if you need it:

| Category | Stages | In the denominator? |
|---|---|---|
| survivors | L3, L4, young adult, adult | yes |
| non-survivors | L1, L2 | yes |
| excluded | egg | no — counted and reported separately |

**survival % = survivors / (survivors + non-survivors) × 100**

Prefer the body-size distribution. It uses no class labels at all, so it is
unaffected by the stage-calling problems below.

**Options:** a list of folders with an optional timepoint for each (one run can
span several timepoints — leave the hours blank to derive them from capture
times), a confidence slider per stage with *Reset to defaults*, **Correct for
uneven class confidence** (on by default — see below), **Count eggs** (off by
default), **Re-analyse images even if results already exist**, and
save-previews.

**"Correct for uneven class confidence" matters more than it sounds.** The model
scores some stages far lower than others, and this option rescales each class's
score before deciding the label. It changes stage assignments — substantially,
for the classes the model is least confident about. It is on by default because
the uncorrected labels are worse, but it is a considered setting rather than a
calibrated one, and the reasoning is recorded in
`launcher/vision/stage_conf.json`.

**Images already analysed are reused.** Before a run starts, the dialog tells
you how many images it can take from previous runs and how many it has to
analyse. Reuse is invalidated automatically whenever a setting that changes the
result changes. Tick **Re-analyse images** to force a fresh pass.

**Grouping is automatic.** If at least 80% of your filenames carry both a dose
and a plate token — `N2_500J_p03_0001.tif` — it groups by filename. Otherwise it
groups by folder. Which it chose is printed in the log and recorded in the
Excel's `run_info` sheet. **The plate is the unit of replication**: per-condition
means and SDs are taken across plates. The one exception is a condition with a
single plate, where the four quadrant images are used instead — that is
technical rather than biological variation, and each row records which was used.

**Output** in `_development_<timestamp>\`: `development_results.xlsx` (sheets
`README`, `run_info`, `per_image`, `per_plate`, `per_condition`, `qc`,
`size_histogram`, `size_summary`), four figures, `explorer.html` for browsing
the detections, `soft_stage_scores.csv`, `log.txt`, and `previews\` if you asked
for them.

**Read `run_info` first.** It records the thresholds, tiling, correction and
model that *actually ran*, echoed back from the inference step rather than from what the
dialog asked for — so a saved result always states how it was produced.

---

<a name="reading-the-output"></a>
## Reading the output — what to trust

This section exists because several of these numbers are easy to misread, and
one of them is currently known to be wrong in a specific way.

### Motility and crawling distances are in PIXELS, not microns

Both pipelines run with `microns_per_pixel = -1.0`. **Every distance is pixels
and every speed is pixels per second.** A column called `speed` is px/s.

This is fine for comparing conditions imaged at the same magnification, and
wrong the moment you compare across magnifications or report an absolute
figure. The body-length-normalised crawling columns cancel magnification out,
but they are still pixel-derived ratios.

The ImageJ calibration you may have set on the Pi scales the **image file** —
it does not reach the tracking data.

**Colony sizes are the exception:** Colony Survival derives µm-per-pixel from
the detected well radius, so colony diameters are genuinely physical.

### Bend rate is a head-angle metric

BPM comes from peaks in a **head-swing angle** — the angle between the head
vector and the body axis, detrended, with peaks over a 0.50 rad prominence.
It is *not* midbody curvature, which is what "body bend" sometimes means
elsewhere. Every row is stamped `bend_method = "head_angle_peaks_v2"`. Compare
against other papers with that in mind.

Worms grouped as "curled" and as "colliding" use slightly different denominators
when converting bends to BPM.

### Development stage calls are provisional

Five things, in descending order of how much they should worry you:

1. **Stage calls skew OLD on mixed plates, and L1/L2 are almost never
   emitted.** On uniform plates carrying one stage the model is good; on plates
   with a spread of stages it calls worms older than they are. The leading
   explanation is a training shortcut — every training tile appears to have
   contained a single stage, so the model never had to discriminate *within* an
   image. **This is open and unresolved.** Treat mixed-stage plates with real
   suspicion.
2. **The survivor cutoff sits on the L2/L3 boundary**, which is the model's
   weakest distinction. So exact percentages are soft even where the direction
   of an effect is robust.
3. **A large minority of L3 calls are the size of an L2.** Measured on a real
   plate set: roughly 30% of L3 calls are at or below the median size of a
   confirmed L2. These are not low-confidence calls — the model is confident —
   so no threshold removes them. They are not corrected, deliberately: an L3
   that is really an L2 must be *counted as a non-survivor*, and deleting it
   would bias the ratio the other way. **Treat the survival percentage as biased
   high**, and prefer the body-size distribution, which uses no labels.
4. **The per-class confidence thresholds are measured but not calibrated.** They
   were derived from the model's own score distribution on a real plate set, so
   they are no longer guesses — but that is not the same as being checked
   against hand-labelled animals, which has not been done. They look as
   authoritative in `run_info` as calibrated ones would.
5. **The class-confidence correction is on by default and is a judgement, not a
   measurement.** It reassigns stages, its strength was chosen by comparing
   against manual counts rather than derived, and a milder setting performed
   about as well. Turn it off to see how much of a result depends on it.

Two worms sitting very close together can still produce one extra box. Known,
not fixed, deliberately — every rule that would suppress it can also delete a
real worm.

### "Analyze on laptop" is not data

The counts drawn on that annotated frame are raw single-image model calls with
no merging or calibration. It is there so you can eyeball a plate at the
microscope. Never put those numbers in a figure.

### Colony counts flag themselves when unreliable

If the stained area covers more than about 55% of the well, the colonies have
merged and counting them is meaningless. The pipeline says so and still reports
the count and the stained area — **check that flag** rather than reading the
count alone.

### Tierpsy is not version-pinned

The container image is pulled as `:latest`, so motility and crawling results are
not guaranteed reproducible across Tierpsy releases. If a run months apart gives
different numbers on the same videos, this is the first thing to suspect.

### The crawling reversal metrics are provisional

Two reversal detectors currently ship side by side — one based on Tierpsy's
motion mode, one on velocity-arrow direction changes. They do not always agree.
The arrow-based columns are explicitly experimental.

---

## Which files to keep

For a result you might publish:

- the source images or videos
- the whole `_analysis_*` / `_survival_*` / `_counting_*` output folder,
  **including `log.txt` and the `run_info` sheet** — those record which model,
  thresholds and software version produced it
- the WormScan version, printed at the top of
  `%APPDATA%\WormScan\launcher.log`

Output folders are self-describing by design. A folder without its log is a
number without a provenance.

---

## Getting help

- `%APPDATA%\WormScan\launcher.log` — the app's own log, starting with its
  version
- `log.txt` inside any output folder — everything that run did
- Troubleshooting for install and connection problems:
  [`launcher/INSTALL.md`](../launcher/INSTALL.md)
