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
| `survival` | full-resolution TIFF still, 4056×3040 | Worm Survival, Colony Survival |

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
run. Re-running is always safe.

### Motility — swimming, from video

Needs a container engine (see `INSTALL.md` step 3). Videos go through Tierpsy
for tracking and skeletonisation, then WormScan's own bend counting.

**Options:** minimum fragment length (default 5 s), clear cache, and optional
video renders (tracked, curvature, side-by-side, per-worm traces).

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

**Output** in `_crawling_analysis_<timestamp>\`: `crawling_results.xlsx`
(`per_worm` and `per_condition` sheets), `crawling_summary.csv`,
`overview.png`, optional renders.

Gives speed, reversals, path tortuosity, activity fractions, and body-length-
normalised versions of the distance metrics.

### Colony Survival — clonogenic assay, from stills

**No container engine needed.** Pure image analysis of crystal-violet stained
single wells: finds the well circle, flattens illumination, thresholds, splits
touching colonies with a watershed, filters by real colony diameter.

**Options:** split sensitivity (default 3.0) and minimum colony size in µm
(default 200).

**Output** in `_counting_analysis_<timestamp>\`: `counting_results.xlsx`
(`per_colony`, `per_plate`, `per_condition`), `counting_summary.csv`,
`overlays\*.png`, `log.txt`.

**Always look at the overlays.** They show exactly what was counted as a
colony. It is the only way to catch a systematically wrong threshold.

### Worm Survival — developmental staging, from stills

**No container engine needed** (it uses the bundled model, not Tierpsy). Each
image is tiled, every tile goes through a YOLO model that classifies worms into
seven stages, and the boxes are merged back together.

Survival is then a ratio of stages:

| Category | Stages | In the denominator? |
|---|---|---|
| survivors | L3, L4, young adult, adult | yes |
| non-survivors | L1, L2 | yes |
| excluded | egg | no — counted and reported separately |

**survival % = survivors / (survivors + non-survivors) × 100**

**Options:** a confidence slider per stage, *Reset to defaults*, a **Count
eggs** tickbox (off by default), and save-previews.

**Grouping is automatic.** If at least 80% of your filenames carry both a dose
and a plate token — `N2_500J_p03_0001.tif` — it groups by filename. Otherwise it
groups by folder. Which it chose is printed in the log and recorded in the
Excel's `run_info` sheet. **The plate is the unit of replication**: per-condition
means and SDs are taken across plates, not across images.

**Output** in `_survival_<timestamp>\`: `worm_survival_results.xlsx` (sheets
`run_info`, `per_image`, `per_plate`, `per_condition`, `dose_response`),
`survival_curve.png`, `log.txt`, and `previews\` if you asked for them.

**Read `run_info` first.** It records the thresholds, tiling and model that
*actually ran*, echoed back from the inference step rather than from what the
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

### Worm Survival numbers are provisional

Four things, in descending order of how much they should worry you:

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
3. **The per-class confidence thresholds were chosen, not calibrated.** They
   look as authoritative in `run_info` as measured ones would.
   `dev/tools/stage_conf_report.py` derives them from a real plate set; until
   that has been run, the numbers are a sensible guess.
4. **It has so far only been validated on training data**, which makes that
   validation partly circular — it confirmed the pipeline works, not that the
   counts are right.

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
