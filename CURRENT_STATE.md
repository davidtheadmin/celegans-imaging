# CURRENT_STATE.md

**Ground-truth snapshot of how the code actually works — regenerated 2026-07-09 from live `HEAD` (`c7045bd`).**

**Updated 2026-07-22:** added §2.6 (the *Analyze on laptop* feature), a matching §6 entry, and corrected §3's thread count (now six, was four). Everything else below still reflects `c7045bd` and has **not** been re-verified since — in particular the worm-survival staging pipeline (`vision/`, `infer_stage.py`, `survival.py`, `SurvivalAgent`) landed after this snapshot and its internals are not yet described here.

This describes the code as it sits in the working tree right now. The tree is
**clean** (no uncommitted/untracked changes); there is one unpushed commit
(`c7045bd`, ahead of `origin/main` by 1) and one stash (`stash@{0}: polish files
in progress`, a doc-only phase-roadmap relabel plus `app.js` button-lockout /
SELECT-hotkey polish — not applied here). Where a comment, docstring, or
`CLAUDE.md` claim no longer matches the code, it is flagged. Trust this document
over `CLAUDE.md`, `README.md`, and your own memory; trust the code over this
document.

---

## 0. Changed since last regeneration

The previous CURRENT_STATE.md was written at commit `e5c3a3d` ("Checkpoint:
crawling pipeline + viewers WIP"). Five commits have landed since, and they
change the launcher and the imaging web UI substantially. Deltas versus the old
file:

1. **The Counting / "Colony Survival" pipeline is LIVE.** The old doc said
   Counting was "permanently `disabled`, 'Not yet built'". It is now a fully
   wired fourth agent. New modules: `launcher/analysis/counting.py` (clonogenic
   colony counter — watershed segmentation of crystal-violet single-well
   stills), `launcher/analysis/counting_agent.py` (agent/status wrapper), and
   `launcher/analysis/crop_wells.py` (well-circle detection, used as a black box
   by counting). The AnalysisDialog exposes it as **"Colony Survival"** (internal
   mode `"counting"`) with two knobs. See §4c. (commit `4a446df`)

2. **The launcher UI is now entirely CustomTkinter, not plain Tkinter.** New
   presentation layer: `launcher/theme.py` (design tokens — a light,
   Apple-style palette + font helpers) and `launcher/widgets.py` (reusable CTk
   widget layer: Cards, IconButton with Segoe Fluent/MDL2 glyphs, tooltips,
   middle-truncation). `launcher/ui.py` was rewritten across Phases 2–3b
   (MainWindow, SettingsDialog, ReviewDialog, both progress dialogs, and the
   AnalysisDialog with a segmented mode picker). The thread/polling/status
   contract is **unchanged** — the new layer is view-only and never touches the
   agents. (commits `669c8f8`, `8fe841d`, `5f4d949`)

3. **The capture web UI got a visual refresh.** `capture/app/static/app.css` was
   restyled (~850 lines rewritten), and two new static assets were added:
   `themes.css` (light/dark theming) and `extras.js`. `index.html` was patched
   to load them. No backend/endpoint change. (commit `c7045bd`)

4. **`config.py` gained two counting fields**: `counting_split_sensitivity`
   (3.0) and `counting_min_colony_um` (200.0).

5. **Tracking status changed.** Everything the old doc called "untracked cruft"
   (`README.md`, `VIEWER_LAUNCHER_SPEC.md`, root `check_*.py` /
   `inspect_filter_decisions*.py`, all of `launcher/tools/`, `contrast.csv`) is
   now **committed and tracked**. The two stray artifacts the old doc mentioned
   (`_saved (…)` dir, `receive each settings update` file) are gone. There is no
   longer any uncommitted work in the analysis/viewers files.

Everything in §2 (Pi capture service) and §4/§4b (motility & crawling internals)
below is carried forward from the prior snapshot and re-verified against live
code — those files have not changed since the last regeneration except as noted.

---

## 1. Repo layout

Top-level, one line each:

- `capture/` — the FastAPI service that runs on the Pi (camera control, capture, file serving, manifests, acks, spatial calibration).
- `capture/capture.py` — standalone full-res still-capture + flat-field CLI script, marked "do not modify". The service imports two functions from it (`apply_flat_field`, `load_master_flat`); the rest is CLI-only.
- `capture/retention.py` — disk-retention daemon, run via systemd timer (`python -m capture.retention`).
- `capture/app/static/` — the web UI. `index.html` + `app.js` + `app.css` (restyled) + **new** `themes.css` (light/dark) + **new** `extras.js`.
- `launcher/` — the Windows CustomTkinter app: syncs files off the Pi, runs the local analysis pipelines, builds grid viewers.
  - `main.py` — entry point; starts four agents (Sync, Motility, Crawling, Counting) and hands all four + their status objects to `MainWindow`.
  - `theme.py` / `widgets.py` — **new** CTk design-token + reusable-widget layers (view-only).
  - `_widget_gallery.py` — **new** standalone dev harness that renders the widget catalogue; not imported by the app (hardcoded sample paths). Dev-only.
  - `ui.py` — MainWindow + Settings/Analysis/Review/progress dialogs (fully CTk).
  - `config.py`, `sync.py` — settings dataclass + background sync thread.
  - `bend_calibration.py` — tracked copy of the bend-method calibration script, with hardcoded `C:\Users\Isabe\…` paths (a near-duplicate of `docs/calibration/bend_calibration.py`).
- `launcher/analysis/` — the analysis pipelines and shared helpers:
  - Motility: `motility.py`, `analysis_csv.py`, `fragment_grouping.py`, `flicker_filter.py`, `plots.py`.
  - Crawling: `crawling.py`, `crawling_metrics.py`, `crawling_fragment_grouping.py`, `crawling_plots.py`, `crawling_render.py`.
  - Counting/colony: `counting.py`, `counting_agent.py`, `crop_wells.py` (**new pipeline**).
  - Shared: `ffmpeg_utils.py`, `docker_utils.py`, `render_video.py`, `concurrency.py`.
- `launcher/viewers/` — two standalone HTML-grid-viewer generators (`make_image_viewer.py`, `make_video_viewer.py`), driven by the launcher's "Review" button.
- `launcher/tools/` — ad-hoc diagnostic scripts, **all now tracked** (`tierpsy_param_sweep.py`, `inspect_skeleton_failures.py`, `inspect_head_angle_spectrum.py`, `compute_shape_metrics.py`, `contrast_analysis.py`, `contrast.csv`, `cut_clip.py`, `worm_stage_preview.py`). None are imported by the app; several carry hardcoded `C:\Users\Isabe\…` paths.
- `deploy/` — three systemd units: capture service, retention oneshot service, retention timer.
- `scripts/` — bash helpers: `deploy.sh` (push→pull→restart), clock sync, data wipe, folder renamers, video mover.
- `docs/calibration/` — the original bend-calibration script + reference PNGs (fast/slow worm examples).
- Root debug scripts (`check_skel_flag.py`, `check_skeletons.py`, `inspect_filter_decisions{,2,3}.py`) — tracked, not imported, hardcoded `C:\Users\Isabe\Desktop\Tierpsyclips\…` paths.
- `CLAUDE.md` — project instructions. **Stale in several places** (see §6).
- `README.md`, `STATUS.md`, `BACKLOG.md`, `VIEWER_LAUNCHER_SPEC.md`, `motility_analysis_spec.md` — narrative/spec docs. `README.md` repeats several `CLAUDE.md` errors (see §6).

The repo holds **two** Tierpsy-parameter JSONs at `launcher/`: `motility_params.json`
and `crawling_params.json`. They differ materially and **intentionally** (see §4.6).

---

## 2. Pi capture service (`capture/app/`)

FastAPI app, served by uvicorn (`app.main:app`). Started by systemd
(`deploy/celegans-capture.service`) with `WorkingDirectory=/home/pi/celegans-imaging/capture`
and `EnvironmentFile=.../capture/.env`. The working directory matters: the static
mount uses the **relative** path `app/static` (`main.py`), so the app only serves
its web UI when launched from `capture/`.

### 2.1 The camera is an IMX477 HQ Camera, not an IMX708 Module 3

`CLAUDE.md` and `README.md` say "Raspberry Pi Camera Module 3 (IMX708)". The code
says otherwise and is internally consistent about it:

- `capture/capture.py` docstring: "Camera: Sony IMX477 HQ Camera (12.3 MP)", sensor 4056×3040.
- `camera.py`: `FULL_W, FULL_H = 4056, 3040` (the IMX477 full array; the IMX708 is 4608×2592).

So full-res frames are 4056×3040. Treat the camera sections in both docs as wrong.

### 2.2 Endpoints

Auth: a single shared bearer token (`auth.require_token`), accepted either as the
`X-Auth-Token` header **or** a `?token=` query param, compared with
`secrets.compare_digest`. Only `/health` is unauthenticated. The MJPEG preview
re-implements the same check inline against the query param because `<img src>`
can't send headers.

- **Top-level (`main.py`):** `GET /health` (no auth), `GET /status` (disk, camera state, unsynced-file stats, `last_retention_run_at`; cached 30 s), sessions CRUD (`POST/GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/plates`, `DELETE /sessions/{id}`), conditions (`DELETE`/`PATCH`/reorder).
- **Camera control (`routers/camera_ctrl.py`, `/camera`):** AE lock/unlock/read, EV-bias read/set (`ExposureValue`, clamped ±3, persisted), spatial calibration CRUD (`/camera/calibration*`, metadata-only, `fov_cm`-based).
- **Preview (`routers/preview.py`):** `GET /preview.mjpg?token=` (MJPEG of lores), `GET /focus` (Laplacian variance of centre crop).
- **Free capture (`routers/free_capture.py`, `/capture/free`):** `POST .../still` (full-res **TIFF**), `POST .../video` (H.264→MP4), list/serve/thumb/delete.
- **Plate capture (`routers/plate_capture.py`):** `POST /sessions/{id}/plates/{plate_id}/capture` branches on `assay_mode` (motility → video; survival → still, optionally per-quadrant, optionally flat-fielded; stills are TIFF).
- **Manifests + acks (`routers/manifest.py`):** `GET /manifest` (the big polled one), scoped manifests, ack endpoints (SHA256 verified; mismatch → 409; traversal → 400/403).
- **System (`routers/system.py`):** `POST /clock-sync` (sudo `date -s`, needs sudoers), `POST /shutdown` (sudo `shutdown -h now`).

### 2.3 Stills are TIFF, optionally ImageJ-calibrated

`save_still` (`capture_ops.py`) writes an **LZW-compressed TIFF**, not a JPEG.
When a spatial calibration is active, the save embeds ImageJ-readable resolution
tags (pixels-per-µm, `unit=um`) so the file opens pre-scaled in microns. The
µm/px value comes from `cam_mgr.active_um_per_px(FULL_W)` — always against the
full-frame width because per-pixel scale is invariant under cropping. Any code or
doc assuming JPEG stills is stale.

### 2.3a session.json, on-disk layout, camera state file

`sessions.py` writes manifests atomically (`.tmp` then `os.replace`). Schema per
`CLAUDE.md`. Session id = `<YYYYMMDDTHHMMSS>_<6-char hash>`; plate `folder_name`
is a computed field `f"{condition_id}_{name}_plate{plate_number:02d}"`. `add_plate`
accepts `replicates` (1–50), rejects `(condition_id, name, plate_number)` collisions with 409.

**Directory names differ from `CLAUDE.md`.** `config.py` defines `experiments/`,
`pictures/`, `videos/` (not `sessions/` + `freecapture/`). Real tree under
`DATA_ROOT` (`/home/pi/celegans-data`):

```
experiments/<session_id>/session.json + plates/<cond>_<name>_plateNN/  (.tif / .mp4)
pictures/<YYYY-MM-DD>/<ts>_still.tif   (+ .sha256, .acked, .thumbs/)
videos/<YYYY-MM-DD>/<ts>_video.mp4
flatfield/master_flat.npy
camera_settings.json   (ev_bias + calibrations + active_calibration; atomic write)
.trash/… ; .retention-last-run
```

Every data file gets a `<name>.sha256` sidecar at capture time; thumbnails cache
under `.thumbs/` (400 px). `.sha256`/`.acked`/`.thumbs/` are excluded from
listings, manifests, retention, and unsynced counts.

### 2.4 Camera concurrency model (`camera.py`)

One global `CameraManager` singleton, one `Picamera2` instance. `_capture_lock`
serialises all state-changing main-stream ops (`capture_still`, recording
start/stop, AE lock/unlock, `set_ev_bias`); `_frame_lock` guards the preview
buffers. A daemon preview thread pulls `capture_array("lores")`, YUV420→BGR,
JPEG-encodes. The deliberate design choice (long comment at `camera.py`) is that
the preview loop does **not** take `_capture_lock` — holding it would block
`start_recording` (which "kicks" picamera2 back into delivering lores frames
after a recording stops), deadlocking. This is the crux of the known concurrency
history — **do not change the locking/threading here.**

Camera starts in a full-res video config (4056×3040 main + 1280×960 lores);
recording flips to 2028×1520 @ 30 fps then back. `_apply_still_controls` re-applies
`FrameDurationLimits` and the persisted EV bias after each return to still mode.
`capture_still()` does a BGR→RGB swap ("libcamera delivers BGR despite RGB888 on
Pi 5"); the standalone `capture.py` that builds the flat does **not** (opposite
channel order; mostly cosmetic — see §6). `set_ev_bias` clamps ±3, default −1.0.
`active_um_per_px(width_px)` = `fov_cm * 10000 / width_px` or None.

### 2.5 Retention daemon (`retention.py`)

Standalone (`python -m capture.retention`), driven by `celegans-retention.timer`.
Re-derives `DATA_ROOT`/dir-names from env. Knobs: `MIN_FREE_GB=5`,
`TARGET_FREE_GB=10`, `MAX_AGE_DAYS=30`, `TRASH_MAX_AGE_DAYS=7`. Reclamation
**permanently deletes** (acked or already-trashed files only); tiered oldest-first.
A capture-time guard (`disk_guard.ensure_capture_space`) refuses captures with
**HTTP 507** if free disk stays below `CELEGANS_CAPTURE_MIN_FREE_GB` (2.0) after a reclaim.

### 2.6 "Analyze on laptop" — full-res frame hand-off (`routers/analyze.py`)

A button in the capture web UI grabs a full-res frame and hands it to the laptop,
where the launcher's vision venv runs the staging model and auto-opens an
annotated PNG plus a counts txt. The transport is a long-poll *from* the laptop
*to* the Pi, so the laptop stays a pure HTTP client with no inbound listener. On
the Pi, `routers/analyze.py` keeps a **single job slot** guarded by an
`asyncio.Condition`, token-authenticated like every other route: `POST /analyze`
captures a frame and parks it — a new press **replaces** any frame still waiting,
so the launcher never processes a stale capture — and the launcher's
`GET /analyze/next` long-poll (25 s, `204` on idle) drains it;
`GET /analyze/status/{jid}` and `POST /analyze/cancel/{jid}` round it out. The
frame is LZW-TIFF-encoded **in memory** and never written to Pi disk.

On the laptop, `launcher/analyze_worker.py` runs `AnalyzeWorker`, a
`threading.Thread` following the same `start()/stop()/join()` lifecycle as the
other agents, but unlike them it carries no status object and is not handed to
`MainWindow` — it just runs and opens its outputs (see the thread list in §3).
Each frame lands in
`Documents\WormScan\analyze_last\<timestamp>\` (frame + `annotated.png` +
`counts.txt`, pruned to the 10 most recent runs) and is shelled out to the vision
venv's `infer_stage.py`, which gained `--draw` and `--counts` flags; its stdout
JSON contract is unchanged, so `survival.py` (which batch-calls the same CLI) is
unaffected.

**Caveat:** the counts drawn on the annotated frame are raw per-image model calls
— adult, and the L2/L3 boundary in particular, are soft — so this is a live QA
aid for eyeballing a plate, **not** a paper number. (Also in §6.)

---

## 3. Windows launcher (`launcher/`)

A **CustomTkinter** desktop app (was plain Tkinter). `main.py` wires up six
background threads — `SyncAgent`, `MotilityAgent`, `CrawlingAgent`,
`CountingAgent`, `SurvivalAgent`, and the UI-less `AnalyzeWorker` (§2.6). The
first five are each paired with a thread-safe status object and handed to
`MainWindow`; `AnalyzeWorker` has no status object and opens its outputs directly.
(Review spawns its own short-lived worker thread on demand.) `SurvivalAgent`'s
pipeline internals are not yet documented here — see the update note at the top.

### 3.1 Thread model (unchanged contract)

Every agent follows the same contract, stated in module docstrings: the **worker
thread only** writes via `status.update()`/`mark_completed()`; the **UI thread
only** reads via `status.snapshot()`/`pop_completed()`. No widget is touched off
the main thread. The UI polls on `root.after()` timers (`_POLL_MS = 2000` main,
`_PROGRESS_POLL_MS = 200` progress dialogs). The new `theme.py`/`widgets.py` layer
is strictly view-side and does not touch this model.

### 3.2 Config (`config.py`)

A `Settings` dataclass persisted to `%APPDATA%\WormScan\config.json`; logs to
`launcher.log` (rotating, 1 MB ×3). Fields/defaults:

- `pi_url = "http://192.168.50.2:8000"`, `token = ""`, `mirror_root = ~/Documents/WormScan`, `poll_interval_s = 120`.
- Analysis: `tierpsy_image = "tierpsy/tierpsy-tracker"`, `tierpsy_image_tag = "latest"`, `docker_command = "docker"`, `analysis_video_timeout_s = 600`, `motility_long_threshold_s = 5.0`, `crawling_min_track_s = 30`.
- **Counting (new): `counting_split_sensitivity = 3.0`, `counting_min_colony_um = 200.0`.**
- `review_type = "auto"`, `review_loop_s = 3.0`, `concurrent_videos = "auto"`.

`load()` filters unknown keys against the dataclass fields, so a stale config
file won't crash the app.

### 3.3 Sync flow (`sync.py`)

Unchanged. `SyncAgent` (daemon thread) cleans leftover `.partial` files, does a
one-shot clock sync, then loops every `poll_interval_s` (or on "Sync now"):
`GET /manifest`, and for every non-acked entry download to
`<dest>.<sha256[:8]>.partial` (stream+hash), verify, `os.replace`, then
`POST .../ack`. `_build_name_maps` mirrors sessions to
`mirror/experiments/<experiment>/<condition>/<plate NN>/<file>` (collision-safe
suffixing), pictures/videos to `mirror/pictures|videos/<date>/…`.

### 3.4 UI structure (`ui.py`) — now CustomTkinter

`MainWindow` shows a status dot + label and buttons: Open Imaging UI, Open
Analysis, Review (Grid Viewer), Open Mirror Folder, Sync now, Shut down Pi,
Settings. Buttons are `widgets.IconButton` with Segoe Fluent/MDL2 glyphs. The
status dot prioritises a running analysis over sync state; "Sync now" locks out
until the next green sync.

`SettingsDialog` edits pi_url/token/mirror/poll-interval (≥10). It does **not**
expose the analysis fields.

`AnalysisDialog` uses a **segmented mode picker** with three live modes:
**Motility**, **Crawling**, and **Colony Survival** (internal `"counting"`).
Counting is no longer disabled. Per-mode UI:

- Motility: folder picker, "Min fragment length (s)" spinbox (1–30), clear-cache checkbox, renders (Tracked / Curvature / Side-by-side / Per-worm traces).
- Crawling: same picker, "Min track span (s)" spinbox (1–600, from `crawling_min_track_s`), renders (Tracked / Side-by-side / Path traces).
- Colony Survival: a "Colony Survival options" card with two knobs — split sensitivity (default 3.0) and min colony µm (default 200.0). **No Docker/ffmpeg/threshold, no cache, no video render** — pure-Python image analysis (§4c).

On Start it validates inputs, runs the mode-appropriate preflight (`run_preflight`
for motility/crawling; `counting_preflight` for counting — the latter only checks
for images + heavy deps, no Docker), persists the mode-appropriate setting, opens
`AnalysisProgressDialog`, and calls the chosen agent's `start_analysis`. Carry-over
rough edges from the old doc remain (the motility "Min fragment length" spinbox is
still validated even when it's inert for another mode; crawling's `threshold_s` is
still passed and only feeds `is_long`, not the gate).

`_on_settings_saved` propagates `update_settings` to all agents. Review
(`ReviewDialog` / `_ReviewProgressDialog` / `_ReviewStatus`) is unchanged (§3.6).

### 3.5 setup.bat / venv / deps

`setup.bat` (in `launcher/`) is additive/idempotent, no admin: Python ≥3.11,
creates `launcher/.venv`, `pip install -r launcher/requirements.txt`, writes a
Desktop `WormScan.lnk` (targets `pythonw.exe "launcher/main.py"`, WorkingDirectory
= repo root, `wormscan.ico`). Dev launch:
`source launcher/.venv/Scripts/activate && python launcher/main.py`.

`launcher/requirements.txt`: requests, pandas, matplotlib, tables, h5py, numpy,
openpyxl, opencv-python, imageio-ffmpeg, scipy, **scikit-image, tifffile,
imagecodecs, customtkinter** (the last four are new — scikit-image/tifffile/
imagecodecs back the counting pipeline + TIFF I/O; customtkinter backs the UI).
`CLAUDE.md`'s "requests (only)" claim is badly stale.

### 3.6 Review (Grid Viewer) feature — unchanged

"Review (Grid Viewer)" opens `ReviewDialog`: pick day folders, content type
(Pictures / Videos / Auto-detect), video loop length. Runs the matching
standalone generator (`launcher/viewers/make_image_viewer.py` /
`make_video_viewer.py`) as a subprocess on a worker thread, UI polls a
lock-guarded `_ReviewStatus`, opens the emitted HTML on exit 0. Generators expect
`"<strain> <dose><unit>"` condition subfolders with `plateNN` children; cache
thumbnails/clips in `.viewer_cache/`.

---

## 4. Motility pipeline — MP4 to Excel

Entry: `MotilityAgent._run_analysis` (`launcher/analysis/motility.py`). Outputs to
`<folder>/_analysis_<YYYY-MM-DD_HHMMSS>/`. **Unchanged since the last snapshot.**

- **Parallel across videos** (`concurrency.resolve_workers`): `ThreadPoolExecutor`, per-video log buffering flushed in completion order, data rows accumulated in discovery order so output is byte-identical to a serial run. `concurrency.py`: `workers = max(1, min(cpus//2, (mem_gb−1.5)//2, 8))`; fallback `(2 cpu, 4 GB)`.
- **Discovery** (`ffmpeg_utils.find_videos`): walks ≤3 levels, skips dirs starting `_`/`.`; depth maps to condition/plate labels.
- **Probe+transcode**: `ffprobe` fps/duration; `convert_to_avi` → MJPEG AVI `-q:v 3`, per-worker `-threads` cap; `CREATE_NO_WINDOW` on Windows.
- **Cache hit** = `<cache>/Results/<stem>_featuresN.hdf5` with `/trajectories_data`. On hit, Tierpsy skipped.
- **Tierpsy** (`docker_utils.run_tierpsy`): `docker run --rm -v <cache>:/data <image> tierpsy_process …`, `<image> = tierpsy/tierpsy-tracker:latest`, timeout `analysis_video_timeout_s` (600 s).

### 4.5 / 4.6 Tierpsy params — two divergent JSONs (intentional)

Both JSONs are written per-video (`expected_fps` patched; WormScan-only
`head_angle_prominence` stripped before Tierpsy). `microns_per_pixel = -1.0` in
both → **all distance/speed metrics are pixels and px/s.** The intentional
divergence (leave as-is):

| key | motility | crawling |
|-----|----------|----------|
| `mask_min_area` | 50 | 500 |
| `thresh_C` | 10 | 5 |
| `thresh_block_size` | 61 | 31 |
| `worm_bw_thresh_factor` | 1.05 | 0.92 |
| `traj_min_area` | 25 | 500 |
| `traj_max_allowed_dist` | 100 | 30 |
| `traj_max_frames_gap` | 10 | 25 |
| `filt_min_displacement` | 0 | 100 |

### 4.7 Bend-rate algorithm

**Bends are counted from a head-swing angle, not `curvature_midbody`.**
`compute_head_angle_signal` (`analysis_csv.py`): `head_vec = skel[0]−skel[5]`,
`body_vec = skel[20]−skel[30]`, signed angle via `atan2(cross, dot)`; <10 valid
frames dropped. Detrend = 0.3 s rolling mean minus 2.0 s baseline. `find_peaks`
on ± signal with `prominence = head_angle_prominence` — effective **0.50 rad**
from JSON (the `0.30` function-signature defaults are **dead**). `bends =
(n_pos+n_neg)/2`, `BPM = bends/duration_min`; curl vs collision groups use
subtly different denominators. Every row is stamped `bend_method =
"head_angle_peaks_v2"` despite a "Bend counter — UNCHANGED from v1 / Do not
modify" comment (comment and label disagree; label wins).

### 4.8–4.11 Grouping, filters, schema, renders

Fragment grouping (`fragment_grouping.py`, curl vs collision via union-find,
`TIME_GAP=5 s`, `DIST=50 px`), flicker filter (`FLICKER_WINDOW=0.5 s`,
`STD_THRESHOLD=20 px`), debris filter (stationary + flickery-blob rules).
`is_long = duration_s >= long_threshold_s` (default 5 s) gates the summary.
`is_full_track` (coverage ≥ 90) is **computed but never read or exported** — dead.
Outputs: `motility_results.xlsx` (per-condition sheets + `_summary`; drops
`member_tierpsy_ids`/`bend_method`/`is_full_track`), `motility_summary.csv`,
`overview.png`, `per_video/*.png` + logs. Optional renders (`render_video.py`,
AVI required): `_tracked.mp4`, `_curvature.mp4` (only place `curvature_midbody`
is used), `_sidebyside.mp4`, per-worm traces (loop var `full_track_rows` filters
on `is_long`, not `is_full_track`). `render_video.py` also holds the
velocity-arrow overlay used by crawling.

---

## 4b. Crawling pipeline

`crawling.py`, `crawling_metrics.py`, `crawling_fragment_grouping.py`,
`crawling_render.py`, `crawling_plots.py`. Outputs to
`<folder>/_crawling_analysis_<timestamp>/`. **Unchanged since the last snapshot.**

- **Grouping**: `crawling_fragment_grouping.link_fragments` — a position-only
  nearest-neighbour stitcher (`T_MAX_S=5`, `D_MAX=150 px`, ambiguity refusal with
  `AMBIG_RATIO=2`, `AMBIG_FLOOR_PX=3`, `AMBIG_TIME_WINDOW_S=1`). **Not** the
  motility engine; no curl/collision classification, no flicker filter. The
  `crawling.py` module docstring calling it "a near-exact copy … same params …
  same output format" is **triply false**.
- **Metrics** (`compute_crawling_metrics`, two passes): head-angle BPM + bend CV,
  speed kinematics (forward/backward/paused), reversals (`motion_mode`, tolerating
  ≤`REVERSAL_PAUSE_TOLERANCE_FRAMES=60` measured pauses), path geometry
  (path/net/tortuosity), activity fractions, CVs. **BL_COLS** — body-length-
  normalized companions dividing each pixel metric by a per-video
  `plate_mean_length_px` scalar. **ARROW_COLS** — velocity-arrow reversal/turn
  detector (`_velocity_arrow_events`, thresholds 140°/60°), explicitly provisional
  ("alongside the existing `reversal_count` for at least one analysis cycle").
- **Quality gate** (`_passes_filter`): `track_duration_s >= min_span_s` **and**
  `skeleton_coverage >= SKELETON_COVERAGE_MIN = 0.70`. `min_span_s` = dialog "Min
  track span (s)" (default 30). `longest_continuous_run_s` is now info-only.
- **Tierpsy** runs through `_run_tierpsy_instrumented` with a **hardcoded
  `_TIERPSY_TIMEOUT_S = 3600`** (ignores `analysis_video_timeout_s`).
- **Outputs**: `crawling_results.xlsx` (per_worm + per_condition),
  `crawling_summary.csv`, `overview.png`, per-video linker sidecars, optional
  renders (`_tracked.mp4` + arrow/reversal overlay, `_sidebyside.mp4`,
  `_path_traces.mp4`).

---

## 4c. Counting / Colony-Survival pipeline (NEW)

Clonogenic colony counter for single-well crystal-violet stills. Entry:
`CountingAgent._run_analysis` (`launcher/analysis/counting_agent.py`), which wraps
the per-image algorithm in `launcher/analysis/counting.py`. Mirrors the
Motility/Crawling agent/status/cancel/progress contract exactly. Heavy deps
(`cv2`, `numpy`, `pandas`, `scikit-image`, `tifffile`) are imported lazily so
launcher startup stays cheap; `counting_preflight()` reports a friendly error if
they're missing. **No Docker, no ffmpeg, no cache, no video render.**

`counting.py` per-image pipeline (`process_image`):

1. Read + detect the circular well (`crop_wells.detect_well`, treated as a black box) → circular analysis mask (count only inside the well).
2. Derive µm/px from the detected radius (or TIFF tags) for physical sizes.
3. Build a stain map (green / OD / gray channel) surfacing faint colonies, then flatten illumination with a large-kernel white-tophat.
4. Threshold inside the mask, then split touching colonies with a distance-transform + h-maxima marker-controlled watershed (not plain connected components).
5. Filter by real colony diameter, well-boundary contact, and solidity.
6. Confluence fallback: if stained-area fraction is high, flag the count unreliable but still report count + stained area.

`CountingOptions` (defaults mirror the CLI): `split_sensitivity=3.0`,
`min_colony_um=200.0`, `well_diameter_mm=34.8`, `stain_channel="od"`,
`threshold="otsu"`, `background_radius_um=3000.0`, `min_solidity=0.5`,
`confluence_frac=0.55`, `mask_shrink=0.96`, `max_depth=3`. The dialog exposes only
`split_sensitivity` and `min_colony_um`; everything else uses defaults.

Discovery mirrors `crawling`/`ffmpeg_utils` (skips `_`/`.` dirs, depth→
condition/plate), extensions `.tif/.tiff/.png/.jpg/.jpeg`, `_MAX_DEPTH=3`.
Outputs to `_counting_analysis_<timestamp>/`: `counting_results.xlsx`
(per_colony / per_plate / per_condition), `counting_summary.csv` (= per_condition),
`overlays/*.png` (manual validation), `log.txt`.

`crop_wells.py` (well detection): finds the well-floor circle by segmenting the
bright growth surface (robust to off-centre plates), with an edge-based Hough
fallback; masks outside the floor. Exposes `detect_well`, `crop_to_well`, and a
TIFF-tag-preserving reader `_read`. Also a standalone CLI.

---

## 5. Configuration surface

**Pi service env vars** (`capture/.env`, prefix `CELEGANS_`): `TOKEN` (required),
`DATA_ROOT` (`/home/pi/celegans-data`), `EXPERIMENTS_DIR`/`PICTURES_DIR`/
`VIDEOS_DIR`, `HOST` (`0.0.0.0`), `PORT` (`8000`), `MAX_AUTO_SHUTTER_US`
(`500000`), `CAPTURE_MIN_FREE_GB` (`2.0`), retention knobs
(`MIN_FREE_GB=5`/`TARGET_FREE_GB=10`/`MAX_AGE_DAYS=30`/`TRASH_MAX_AGE_DAYS=7`).
`.env.example` is the committed template.

**Persistent camera state** (`<DATA_ROOT>/camera_settings.json`): `ev_bias`
(default −1.0, clamp ±3), `calibrations`, `active_calibration`.

**Launcher config** (`%APPDATA%\WormScan\config.json`): see §3.2.

**Hardcoded paths / magic constants:** Pi `192.168.50.2` / laptop `192.168.50.1`
/ SSH alias `celegans`; camera `FULL=4056×3040`, `VIDEO=2028×1520@30`,
`PREVIEW=1280×960`, bitrate `9_000_000`, capture `30 s`, EV −1.0; skeletons
`resampling_N=49`, head-angle indices 0/5 & 20/30, detrend 0.3 s/2.0 s, prominence
0.50 rad; motility distance 50 px / gap 5 s / min-obs 10 s / collision cap 3;
crawling `MIN_SPAN_S=30`, `SKELETON_COVERAGE_MIN=0.70`, linker `D_MAX=150`/
`T_MAX_S=5`, arrow 140°/60°; counting `well_diameter_mm=34.8`,
`background_radius_um=3000`, `confluence_frac=0.55`; `_MAX_WORKERS=8`; render
libx264 crf 22. Segoe font files `C:\Windows\Fonts\SegoeIcons.ttf` /
`segmdl2.ttf` are probed by `widgets.py`.

**Auth:** one shared token, `secrets.compare_digest`, header or query param.

---

## 6. Known deviations and rough edges

Roughly ordered by how likely they are to bite you.

1. **`CLAUDE.md` and `README.md` camera identity is wrong.** Hardware is IMX477 HQ Camera (4056×3040), confirmed by `capture.py`/`camera.py`. (§2.1)
2. **`CLAUDE.md` on-disk layout is wrong.** Real names `experiments/`/`pictures/`/`videos/`; `camera_settings.json` is undocumented there. (§2.3a)
3. **Stills are TIFF, not JPEG.** (§2.3)
4. **Flat-field directory mismatch — likely a real, still-present bug.** The service reads the master flat from `DATA_ROOT/flatfield` but its error message tells you to run `capture/capture.py --capture-flat`, whose default `FF_DIR` is `<repo>/data/flatfield`. Following the instructions writes a flat the service can't find. Dormant because flat-field is opt-in per request.
5. **BGR/RGB asymmetry** between `capture_still` (swaps) and the flat-building `capture.py` (doesn't). Mostly cosmetic (flat is per-channel normalised). (§2.4)
6. **Bend rate is a head-angle metric, not curvature** (prominence 0.50 rad). (§4.7)
7. **`head_angle_prominence` function-signature defaults (0.30) are dead**; real value 0.50 from JSON. (§4.7)
8. **"Bend counter UNCHANGED from v1 / Do not modify" comment vs `bend_method="head_angle_peaks_v2"` label** disagree; label wins. (§4.7)
9. **Curl vs collision BPM use different denominators.** (§4.7)
10. **`crawling.py` module docstring is triply false** (params, engine, output schema all differ from motility). (§4b)
11. **`is_full_track` (motility) computed but never used/exported.** Dead field; per-worm-trace render var `full_track_rows` filters on `is_long`. (§4.8–4.11)
12. **Motility Excel drops computed columns** (`member_tierpsy_ids`, `bend_method`, `is_full_track`).
13. **Crawling Tierpsy timeout hardcoded to 3600 s**, ignoring `analysis_video_timeout_s`. (§4b)
14. **Crawling `threshold_s` half-used** (feeds `is_long`, not the gate); motility min-fragment spinbox validated even when inert. (§3.4, §4b)
15. **Velocity-arrow reversal/turn columns are explicitly provisional** — two parallel reversal metrics ship today. (§4b)
16. **Static file mount is CWD-relative** — only correct because systemd sets `WorkingDirectory=.../capture`.
17. **`microns_per_pixel = -1.0`: analysis outputs are pixels / px/s.** The BL-normalized crawling columns cancel plate magnification but are still pixel-derived; the ImageJ TIFF calibration scales the *image file*, not the HDF5 features. Counting *does* derive µm/px from the detected well radius, so colony sizes are physical.
18. **`CLAUDE.md`/`README.md` say the launcher needs "requests (only)".** It needs the full scientific stack plus scikit-image, tifffile, imagecodecs, customtkinter. (§3.5)
19. **Mixed async/sync in the session router** (`create_session`/`add_plate` run on the event loop; the rest use `asyncio.to_thread`).
20. **`clock-sync` and `shutdown` depend on sudoers entries.**
21. **`bend_calibration.py` is duplicated and tracked twice** — `docs/calibration/` and `launcher/`, the latter with hardcoded `C:\Users\Isabe\Documents\WormScan\…` manual-count paths.
22. **`CLAUDE.md` phase roadmap is stale** — it lists "Phase 4 Analysis service / Phase 5 Web UI" as future, but the launcher-hosted analysis (motility, crawling, counting) and both web UIs are built. The stash carries a corrected roadmap that hasn't been applied.
23. **Tierpsy Docker image is pinned to `:latest`** — analysis is not reproducible across Tierpsy releases. (see AUDIT)
24. **Counting `crop_wells` is treated as a validated black box** by `counting.py`/`counting_agent.py`; there is no automated regression test guarding either.
25. **"Analyze on laptop" counts are a QA aid, not data.** The annotated-frame counts from `routers/analyze.py` → `infer_stage.py --draw/--counts` are raw per-image model calls with soft adult / L2–L3 boundaries — use them for live eyeballing, never as reported figures. (§2.6)
