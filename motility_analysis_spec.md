# Motility Analysis — Module Specification for Claude Code

## Context

WormScan needs an automated motility analysis pipeline that processes captured C. elegans
videos and produces bend-rate measurements suitable for survival/motility assays. This work
extends the existing `launcher/` desktop application and uses the validated Tierpsy Tracker
pipeline running in Docker on the laptop.

The pipeline has been **manually verified end-to-end** on a real video: 30 s clip at 11.72 fps
of ~15 worms, producing 8 fully-tracked worms with bend rates clustering around 50-60 BPM.
The exact Tierpsy parameter set that achieves this is shipped in this spec (Section 7).

## Goals

1. Wire up the existing **"Open Analysis"** button in the launcher UI (currently disabled with
   a "Not yet built" tooltip).
2. Add an analysis-type selector: **Motility** (this spec) and **Counting** (placeholder for
   future work).
3. For Motility: take a folder of videos, run them through Tierpsy in headless Docker, produce
   a per-fragment CSV, a per-video summary CSV, per-video summary plots, and an overview plot
   grouping by condition.
4. Preserve the existing launcher's threading discipline: long-running work happens on a
   background thread, the UI polls a shared status object via `root.after()`, no Tk widgets
   are touched outside the main thread.

## Non-goals (v1)

- No GPU usage. Tierpsy CPU pipeline only.
- No re-analysis with different parameters from inside the launcher. Parameters are frozen.
- No microns_per_pixel calibration. Speed/length values stay in pixel units; bend rate is
  dimensionless and unaffected.
- No running container management. Each analysis run uses ephemeral `docker run --rm`
  containers. The user just needs Docker Desktop installed and the `tierpsy/tierpsy-tracker`
  image pulled.
- No counting/survival analysis. That branch of the UI exists as a stub only.

## Architecture

### File layout (additions to `launcher/`)

```
launcher/
├── analysis/                   # NEW
│   ├── __init__.py
│   ├── motility.py             # MotilityAgent + MotilityStatus + run_video()
│   ├── ffmpeg_utils.py         # convert_to_avi(), probe_fps()
│   ├── docker_utils.py         # run_tierpsy(), check_docker(), check_image()
│   ├── analysis_csv.py         # bends_per_minute(), build_csvs()
│   └── plots.py                # make_video_summary_png(), make_overview_png()
├── motility_params.json        # NEW — frozen Tierpsy parameter set (Section 7)
├── ui.py                       # MODIFIED — wire up "Open Analysis" button
├── main.py                     # MODIFIED — instantiate MotilityAgent, wire to UI
├── config.py                   # MODIFIED — add Settings.docker_image and Settings.tierpsy_image_tag
└── requirements.txt            # MODIFIED — add pandas, matplotlib, tables, h5py
```

### Threading model

Mirror the existing `SyncAgent` / `SyncStatus` pattern from `sync.py` exactly:

- `MotilityStatus` — shared state, accessed from UI via `snapshot()`, written from worker via
  `update()`. Internal `threading.Lock`. No widget access from worker.
- `MotilityAgent(threading.Thread)` — daemon thread. Idle by default, woken by
  `start_analysis(folder)`. Stops cleanly on `stop()`.
- UI polls `MotilityStatus.snapshot()` via the existing `_poll()` loop in `MainWindow` (extend
  it to also refresh motility status fields).

This means a long analysis run cannot freeze the UI, and the user can keep using sync etc.
while motility analysis runs in the background.

### User flow

1. User clicks **Open Analysis** (now enabled).
2. A small dialog opens: radio buttons for **Motility** / **Counting** (latter disabled with
   "Not yet built" tooltip), plus a folder picker (defaulting to the mirror folder).
3. User picks Motility, picks a folder, clicks **Start**.
4. Dialog closes. The status row in the main window now shows motility progress:
   "Analysing: condition_X / video_Y.mp4 (3 of 12)" etc.
5. When complete: status returns to normal, a message box appears: "Motility analysis complete:
   N videos processed, M failed. Open results folder?" with **Open** / **OK** buttons.

If the user clicks Open Analysis again while a run is in progress, the dialog should warn and
not start a second run.

## Inputs

### Folder structure expected

The user picks a top-level folder. Any of the following layouts are valid:

```
<selected_folder>/                    <- option A: flat, single condition
├── plate1.mp4
├── plate2.mp4
└── plate3.mp4

<selected_folder>/                    <- option B: condition subfolders (typical)
├── N2_0J/
│   ├── plate1.mp4
│   └── plate2.mp4
├── N2_50J/
│   └── plate1.mp4
└── daf16_0J/
    └── plate1.mp4

<selected_folder>/                    <- option C: matches WormScan capture sync output
└── 2026-05-01_UV_pilot/
    ├── N2_0J/
    └── N2_50J/
```

Discovery rule: recursively find all `.mp4` files **up to 3 levels deep** from the selected
folder. Hard-cap at 3 to prevent accidental "analyse my whole Documents folder" disasters.

Condition labelling: the **immediate parent folder name** of each video is its condition.
If the video sits directly in the selected folder, condition = `"unlabeled"`.

Plate labelling: the video filename without extension.

### Video format expectations

Any `.mp4` file from the Pi capture system. Pipeline auto-converts to MJPEG AVI as the first
step, so frame-rate, codec, and resolution variations are handled by the converter, not by
downstream code.

If a `.avi` already exists alongside a `.mp4` (i.e. a previous run), the conversion step is
skipped on the second invocation.

## Outputs

All outputs go into `<selected_folder>/_analysis/<timestamp>/` where timestamp is the run
start in `YYYY-MM-DD_HHMMSS` format. The leading underscore keeps `_analysis` at the top of
file listings on Windows.

```
<selected_folder>/_analysis/2026-05-01_153012/
├── motility_results.csv         # per-fragment rows
├── motility_summary.csv         # per-video rows
├── overview.png                 # box-and-whisker BPM by condition
├── log.txt                      # full run log including Tierpsy stderr per video
└── per_video/
    ├── N2_0J__plate1.png        # double-underscore separates condition from plate
    └── ...
```

### `motility_results.csv` columns

One row per Tierpsy trajectory fragment with valid bend data.

| column          | type    | description                                              |
|-----------------|---------|----------------------------------------------------------|
| condition       | str     | parent folder name, or "unlabeled"                       |
| plate           | str     | video filename without extension                         |
| worm_index      | int     | Tierpsy trajectory ID (not a unique worm — see note below) |
| frames          | int     | number of frames in this fragment                        |
| duration_s      | float   | duration in seconds (frames / fps)                       |
| bpm             | float   | bends per minute                                         |
| is_long         | bool    | True if duration_s >= 5.0                                |
| fps_used        | float   | actual fps from ffprobe                                  |

### `motility_summary.csv` columns

One row per video.

| column            | type    | description                                              |
|-------------------|---------|----------------------------------------------------------|
| condition         | str     | parent folder name                                       |
| plate             | str     | video filename without extension                         |
| n_fragments_total | int     | total Tierpsy trajectory fragments                       |
| n_fragments_long  | int     | fragments with duration >= 5.0 s                         |
| bpm_median_long   | float   | median BPM across long fragments (the headline metric)   |
| bpm_mean_long     | float   | mean BPM across long fragments                           |
| bpm_std_long      | float   | stdev BPM across long fragments                          |
| bpm_min_long      | float   | minimum BPM (long fragments)                             |
| bpm_max_long      | float   | maximum BPM (long fragments)                             |
| fps_used          | float   | actual fps from ffprobe                                  |
| duration_video_s  | float   | full video duration                                      |
| status            | str     | "ok" or short error message                              |

### Note on `worm_index`

Tierpsy assigns a new `worm_index` whenever a track is lost and re-acquired. With ~15 worms
in a dish over 30 s, expect roughly 100 fragments — most are short stubs from worms briefly
crossing or coiling. The fully-tracked fragments (those that span the entire video) are the
high-confidence measurements. The `is_long` flag (>= 5 s) is the conservative cut for "use
this row in summaries." Per-video summary uses **long fragments only**, ignoring the stubs.

This is documented behaviour, not a bug. Don't try to re-stitch fragments into per-physical-
worm tracks — Tierpsy can't disambiguate when worms touch and that path leads to wrong numbers.

### Plots

**Per-video PNG** (`<condition>__<plate>.png`): two panels side by side.
1. Bar chart of BPM per long fragment, sorted ascending, with median line.
2. Curvature trace over time for the 3 longest fragments — sine-wave signal showing the
   bends being counted, zero line marked.

This panel is the "show your coworker" plot. The curvature trace is the visually convincing
bit — viewers can see the wiggle and see why each zero-crossing is half a bend.

**Overview PNG** (`overview.png`): one figure per analysis run.
- Box-and-whisker plot of `bpm_median_long` per video, grouped by condition.
- Each video is one point overlaid on the boxes.
- N videos shown per condition.
- Title includes total run time and total videos processed.

If only one condition is present (option A folder layout), the overview falls back to a single
bar chart of `bpm_median_long` per video.

## Pipeline steps (per video)

For each `.mp4` discovered:

1. **Probe fps with ffprobe.** Use `ffprobe -v error -select_streams v:0 -show_entries
   stream=r_frame_rate -of default=nw=1:nk=1 <video.mp4>`. Returns something like `30/1` or
   `587/50`. Evaluate as a float. If it fails, log warning and skip the video with status
   "ffprobe failed".

2. **Convert to AVI** if not already converted. Use:
   ```
   ffmpeg -y -i <video.mp4> -vcodec mjpeg -q:v 3 <video.avi>
   ```
   No scaling — keep native resolution. The Pi captures at a consistent resolution, and the
   manual run validated this works. If ffmpeg fails, skip with status "ffmpeg failed".

3. **Write per-video params JSON** next to the video, based on `motility_params.json` template
   but with `expected_fps` set to the probed value. Filename: `<video_basename>.json`. This
   matches Tierpsy's convention of "JSON beside video".

4. **Run Tierpsy headlessly via Docker.** Use:
   ```
   docker run --rm \
     -v "<video_parent_folder>:/data" \
     tierpsy/tierpsy-tracker \
     tierpsy_process \
     --video_dir_root   /data \
     --mask_dir_root    /data/MaskedVideos \
     --results_dir_root /data/Results \
     --pattern_include  <video>.avi \
     --json_file        /data/<video>.json \
     --max_num_process  1
   ```
   `tierpsy_process` is batch-oriented and scans `--video_dir_root` for files
   matching `--pattern_include`; it does not accept a single `--video_file`.
   Passing the basename as the pattern isolates processing to one file per run.
   On Windows, paths must be passed in a Docker-compatible form. Capture stdout
   and stderr to the run log. Time out after 10 minutes per video.

5. **Read `_featuresN.hdf5`** from the Results folder. Compute per-fragment BPM using the
   algorithm in Section 6.

6. **Update status** with progress: `(current_video_index, total_videos, video_basename)`.

7. **Continue on error.** If any single step fails for a video, log it, mark `status` for
   that video, and continue with the next. Don't abort the batch.

After all videos processed:

8. **Write CSVs.** `motility_results.csv` with all fragment rows from all videos;
   `motility_summary.csv` with one row per video (status="ok" for successful, error message
   for failed).

9. **Write plots.** Per-video PNGs for successful videos. Overview PNG.

10. **Final status update.** Set color="green", label="Analysis complete: N/M videos".

## Bend-counting algorithm (validated, do not change)

Implemented in `analysis_csv.py`. Operates on the `timeseries_data` table from
`_featuresN.hdf5`.

```python
import numpy as np
import pandas as pd

def bends_per_minute(group: pd.DataFrame, fps: float) -> float | None:
    """
    Compute bends per minute for a single Tierpsy trajectory fragment.

    A bend = one full sinusoidal cycle of midbody curvature, equivalent to
    two zero-crossings of the (smoothed) curvature signal.

    Returns None if the fragment is too short (<10 valid samples) to give a
    meaningful rate.
    """
    sig = group['curvature_midbody'].dropna().values
    if len(sig) < 10:
        return None
    # Smooth with a ~0.3 s rolling mean to suppress noise-driven crossings
    win = max(3, int(fps * 0.3) | 1)   # force odd window length
    sig = pd.Series(sig).rolling(win, center=True, min_periods=1).mean().values
    crossings = int(np.sum(np.diff(np.sign(sig)) != 0))
    bends = crossings / 2
    duration_min = len(sig) / fps / 60
    return bends / duration_min
```

This function and the value of `0.3` for the smoothing window were validated against the
manual run that produced 56 BPM median across 13 long fragments — biologically sensible for
crawling C. elegans. Do not change the algorithm in v1 without re-validation against the
reference video.

The "long fragment" threshold for summary statistics is **5.0 seconds**.

## Frozen Tierpsy parameter set

Ship `launcher/motility_params.json` with these contents. They are the fully-debugged set
that produced clean tracking on the reference video (white worms on dark background, 1080p,
~15 worms per frame).

Key non-default values and why each matters:

| parameter                | value | reason                                                    |
|--------------------------|-------|-----------------------------------------------------------|
| `is_light_background`    | false | Critical: defaults assume dark worms on light. Inverting was the first major fix. |
| `use_nn_food_cnt`        | false | Critical: NN food detector was rejecting all worms because no clear bacterial lawn. |
| `nn_filter_to_use`       | "none" | Skip the worm-vs-debris NN classifier (training data mismatch). |
| `analysis_type`          | "BASE" | Don't use TIERPSY/TIERPSY_FEATURES presets — they enable the NN filter. |
| `traj_max_allowed_dist`  | 100   | Default 25 was too strict for fast-moving worms at ~12 fps; allow 100 px between frames. |
| `traj_max_frames_gap`    | 5     | Default 0 broke trajectories on every single dropped frame. |
| `filt_min_displacement`  | 0     | Default 10 rejected all stationary or near-stationary worms. |
| `filt_bad_seg_thresh`    | 0.1   | Default 0.8 rejected fragments with imperfect skeletons; 0.1 is forgiving. |
| `expected_fps`           | (set per video at runtime) | From ffprobe on each video. |
| `microns_per_pixel`      | -1.0  | Pixel units only in v1. Add calibration later. |

The full JSON ships at `launcher/motility_params.json`. The agent loads it once at startup,
sets `expected_fps` per video, writes the per-video copy next to each video.

## Settings additions

Add to `Settings` dataclass in `config.py`:

```python
@dataclass
class Settings:
    pi_url: str = "http://192.168.50.2:8000"
    token: str = ""
    mirror_root: str = str(_DEFAULT_MIRROR)
    poll_interval_s: int = 120
    # Analysis-related
    tierpsy_image: str = "tierpsy/tierpsy-tracker"
    tierpsy_image_tag: str = "latest"
    docker_command: str = "docker"           # allow override for podman / docker.exe full path
    analysis_video_timeout_s: int = 600      # 10 min cap per video
```

The Settings dialog does not need new fields in v1 — defaults are correct. Power users can
edit `config.json` directly.

## Pre-flight checks

Before starting an analysis run, the agent verifies:

1. `docker --version` runs successfully → if not, fail with "Docker not installed or not in PATH".
2. `docker info` succeeds → if not, fail with "Docker is installed but not running. Start Docker Desktop."
3. `docker image inspect tierpsy/tierpsy-tracker` succeeds → if not, fail with "Tierpsy image not pulled. Run: docker pull tierpsy/tierpsy-tracker".
4. `ffmpeg -version` runs → if not, fail with "ffmpeg not installed or not in PATH. Install with: winget install Gyan.FFmpeg".
5. `ffprobe -version` runs → if not, fail with same install hint.
6. The selected folder contains at least one `.mp4` file (recursive, max depth 3).

All check failures surface as a single error dialog and abort before any analysis runs.

## Error handling rules

- **Per-video errors**: log to `log.txt`, set `status` field in summary CSV to a short
  human-readable message, continue with next video.
- **Pre-flight failures**: error dialog, no run starts.
- **Crash inside `MotilityAgent.run()`**: log full traceback, update status to red
  ("Analysis crashed — see log"), do not propagate to UI thread.
- **User closes the launcher mid-run**: `MotilityAgent.stop()` sets the stop event;
  the current `docker run` subprocess is left to finish (it'll be killed when the parent
  Python process exits, which Docker handles gracefully via `--rm`). Half-written CSVs are
  left in place — better to have partial results than nothing.

## Validation

Before merging, validate against the reference video used in spec development:

- **Reference video**: `test.avi` from manual run, 30 s, ~15 worms, white-on-dark.
- **Expected output**: ~107 total fragments, ~19 with valid BPM, ~13 long fragments with
  median BPM in the range **50-65** and stdev < 20.
- **Spot-check**: open the per-video PNG and confirm the curvature trace shows clear
  oscillations crossing zero ~1 Hz.

If numbers drift outside this range, something has changed in the pipeline — investigate
before merging.

## Out of scope, but worth noting for future work

- **Microns-per-pixel calibration**: a small "Calibrate" button in the analysis dialog that
  takes a calibration image (ruler, micrometer) and computes pixel scale. Once added, the
  motility CSV gains real-units columns: `length_um`, `mean_speed_um_s`, `width_midbody_um`.
- **Re-stitching fragments**: optional post-processing that links fragments by spatial
  proximity at start/end frames. Useful for per-worm metrics but error-prone; opt-in only.
- **Counting analysis**: the second branch of the analysis dialog. Will use YOLOv8 inference
  on still images for L1-L4 staging — entirely separate pipeline, separate model file, no
  Docker required (ultralytics on the laptop directly).
- **Comparison report**: a "compare two analysis runs" view, useful for before/after
  treatments. Pure UI work on top of the existing CSVs.
- **Direct integration with capture sessions**: instead of folder picker, "select session"
  dropdown that knows about the manifest from `sync.py` and finds the right folder
  automatically.
