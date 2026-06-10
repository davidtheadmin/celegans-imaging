# CURRENT_STATE.md

**Ground-truth snapshot of how the code actually works — generated 2026-06-04.**

This describes the code as it sits in the working tree right now (including the
uncommitted changes to the crawling pipeline and the viewers), not the design
intent and not the history. Where a comment, docstring, or `CLAUDE.md` claim no
longer matches the code, it is flagged. If you are reasoning about this system
from an older conversation, trust this document over your memory.

Two blunt heads-ups before anything else:

1. **`CLAUDE.md` is wrong about the camera, wrong about the on-disk data layout,
   and wrong about the launcher's dependencies.** It also predates the spatial
   calibration system, the EV-bias control, and the switch from JPEG to TIFF
   stills. Don't anchor on it.
2. **The crawling pipeline has been substantially rewritten since the last
   snapshot (2026-05-27).** It no longer shares motility's grouping engine, it
   computes a large new metric set (body-length-normalized speeds, activity
   fractions, velocity-arrow reversal/turn detection), and its quality gate is
   now span + coverage rather than "longest unbroken run". Both pipelines now run
   videos in parallel. Details in §4b.

---

## 1. Repo layout

Top-level, one line each:

- `capture/` — the FastAPI service that runs on the Pi (camera control, capture, file serving, manifests, acks, spatial calibration).
- `capture/capture.py` — standalone full-res still-capture + flat-field CLI script, marked "do not modify". The service imports two functions from it (`apply_flat_field`, `load_master_flat`); the rest is CLI-only.
- `capture/retention.py` — disk-retention daemon, run via systemd timer (`python -m capture.retention`).
- `launcher/` — the Windows Tkinter app: syncs files off the Pi, runs the local analysis pipelines, and builds grid viewers.
- `launcher/analysis/` — the motility and crawling analysis pipelines plus the shared ffmpeg/Docker/render helpers and the new `concurrency.py` autosizer.
- `launcher/viewers/` — **new**: two standalone HTML-grid-viewer generators (`make_image_viewer.py`, `make_video_viewer.py`) driven by the launcher's "Review" button.
- `launcher/bend_calibration.py` — **new tracked copy** of the bend-method calibration script, with hardcoded `C:\Users\Isabe\…` paths (a duplicate of `docs/calibration/bend_calibration.py`).
- `launcher/tools/` — ad-hoc diagnostic scripts. **Partially tracked now**: `tierpsy_param_sweep.py` is checked in; the rest (`compute_shape_metrics.py`, `contrast_analysis.py`, `cut_clip.py`, `inspect_*`, `worm_stage_preview.py`, `contrast.csv`) are untracked. None are imported by the app.
- `deploy/` — three systemd units: capture service, retention oneshot service, retention timer.
- `scripts/` — bash helpers: `deploy.sh` (push→pull→restart), clock sync, data wipe, folder renamers.
- `docs/calibration/` — the original bend-calibration script + reference PNGs (fast/slow worm examples).
- `CLAUDE.md` — project instructions. **Stale in several places** (see §6).
- `STATUS.md`, `BACKLOG.md`, `motility_analysis_spec.md` — narrative/spec docs (not covered here per scope).
- Untracked top-level cruft: `README.md`, `VIEWER_LAUNCHER_SPEC.md` (both new and untracked), the root `check_*.py` / `inspect_filter_decisions*.py` debugging scripts (hardcoded `C:\Users\Isabe\Desktop\Tierpsyclips\…` paths), plus two oddly-named untracked artifacts in the repo root — a directory `_saved (previously only sync and motility were updated)` and a file `receive each settings update`. Throwaway; ignore.

The repo holds **two** Tierpsy-parameter JSONs at `launcher/`: `motility_params.json`
and `crawling_params.json`. They differ materially (see §4.6).

---

## 2. Pi capture service (`capture/app/`)

FastAPI app, served by uvicorn (`app.main:app`). Started by systemd
(`deploy/celegans-capture.service`) with `WorkingDirectory=/home/pi/celegans-imaging/capture`
and `EnvironmentFile=.../capture/.env`. The working directory matters: the static
mount uses the **relative** path `app/static` (`main.py:176`), so the app only
serves its web UI when launched from `capture/`.

### 2.1 The camera is an IMX477 HQ Camera, not an IMX708 Module 3

`CLAUDE.md` says "Raspberry Pi Camera Module 3 (IMX708)". The code says otherwise
and is internally consistent about it:

- `capture/capture.py` docstring: "Camera: Sony IMX477 HQ Camera (12.3 MP)", sensor 4056×3040.
- `camera.py`: `FULL_W, FULL_H = 4056, 3040` (the IMX477 full array; the IMX708 is 4608×2592).

So full-res frames are 4056×3040. Treat the `CLAUDE.md` camera section as wrong.

(There is still a smaller lie inside `camera.py:21`: the comment on the video
resolution says `IMX477 mode 2: … 53.77 fps max`, while the next constants lock
the stream to 30 fps. The 53.77 figure is informational and unused.)

### 2.2 Endpoints

Auth model: a single shared bearer token (`auth.require_token`), accepted either
as the `X-Auth-Token` header **or** a `?token=` query param, compared with
`secrets.compare_digest`. Only `/health` is unauthenticated. The MJPEG preview
re-implements the same check inline against the query param because `<img src>`
can't send headers.

**Top-level (`main.py`):**
- `GET /health` — `{"status":"ok","schema_version":1}`, no auth.
- `GET /status` — disk free/total GB, `data_root`, `camera_ready`, `ae_locked`, unsynced-file stats, and `last_retention_run_at` (mtime of `.retention-last-run` marker). Unsynced stats walk `experiments/`, `pictures/`, `videos/` and count files that have no sibling `.acked`, aren't dotfiles, aren't under `.thumbs/`, and aren't `.sha256`/`.acked`; cached 30 s (`_STATUS_CACHE_TTL`).
- Sessions: `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/plates`, `DELETE /sessions/{id}`.
- Conditions: `DELETE /sessions/{id}/conditions/{condition_id}?name=…`, plus **two newer endpoints** — `POST /sessions/{id}/conditions/reorder` (body `{order: [{condition_id, name}]}`) and `PATCH /sessions/{id}/conditions/{condition_id}?name=…` (body `{strain_label?, treatment_label?}`) for renaming. These back the launcher-side bulk-add/reorder/rename UI and did not exist at the last snapshot.

**Camera control (`routers/camera_ctrl.py`, prefix `/camera`):**
- `POST /camera/ae/lock` — reads current `ExposureTime`/`AnalogueGain` from a capture request's metadata, disables AE, pins those values. Returns the locked exposure/gain.
- `POST /camera/ae/unlock` — re-enables AE.
- `GET /camera/exposure` — current lock state + pinned values.
- **`GET /camera/ev` / `POST /camera/ev`** — read/set the AE exposure-compensation bias (`{value: float}`), clamped to `[-3.0, 3.0]`, applied as the libcamera `ExposureValue` control, and persisted (see §2.3a). New since the last snapshot.
- **Spatial calibration (`/camera/calibration*`)** — `GET` lists stored calibrations + the active one; `POST` upserts `{label, fov_cm}` (rejects non-positive `fov_cm`, makes the label active); `POST /camera/calibration/active` switches the active label; `DELETE /camera/calibration/{label}` removes one. These are metadata-only and do **not** require the camera to be ready. New since the last snapshot.

**Preview (`routers/preview.py`):**
- `GET /preview.mjpg?token=…` — multipart MJPEG stream of the lores frames; yields only when the JPEG changes, else sleeps.
- `GET /focus` — Laplacian variance of the centre crop of the latest lores frame. Higher = sharper.

**Free capture (`routers/free_capture.py`, prefix `/capture/free`):**
- `POST /capture/free/still` — full-res **TIFF** to `pictures/<YYYY-MM-DD>/<ts>_still.tif`. Optional flat-field correction. **(Stills are now TIFF, not JPEG — see §2.3 and §6.)**
- `POST /capture/free/video` — H.264 recording for `duration_s`, remuxed to MP4, saved to `videos/<YYYY-MM-DD>/`.
- `GET /capture/free/files`, `GET /capture/free/videos` — list a day's stills/videos.
- `GET .../files/{date}/{filename}` and `.../videos/{date}/{filename}` — serve a file, or its thumbnail with `?thumb=1`.
- `DELETE` variants — move file to `.trash/`.

**Plate capture (`routers/plate_capture.py`):**
- `POST /sessions/{id}/plates/{plate_id}/capture` — branches on `session.assay_mode`: motility records a video; survival captures a still (optionally per-quadrant, optionally flat-fielded). Survival stills are TIFF as well.
- `GET .../files` and `.../files/{filename}` (with `?thumb=`) — list/serve plate files.
- `DELETE .../{plate_id}` and `DELETE .../files/{filename}` — trash a plate or single file.

**Manifests + acks (`routers/manifest.py`):**
- `GET /manifest` — the big one the launcher polls: all sessions' file manifests plus `pictures` and `videos` manifests, gathered concurrently via `asyncio.gather`.
- Scoped manifests: `GET /sessions/{id}/manifest`, `GET /capture/free/manifest`, `GET /capture/free/videos/manifest`.
- Acks: `POST /sessions/{id}/files/ack`, `POST /capture/free/files/ack`, `POST /capture/free/videos/ack` — client confirms a verified copy; server checks the supplied SHA256 against the stored one and writes a `<file>.acked` marker. SHA256 mismatch → 409; path traversal → 400/403.

**System (`routers/system.py`):**
- `POST /clock-sync` — client posts ISO time; server computes the offset, refuses if it deviates >5 years, otherwise runs `sudo -n date -s …`. Needs a sudoers entry for `/bin/date`.
- `POST /shutdown` — fires `sudo /sbin/shutdown -h now` via `Popen`, returns 202.

### 2.3 Stills are TIFF, optionally ImageJ-calibrated

`save_still` (`capture_ops.py`) writes an **LZW-compressed TIFF**, not a JPEG.
Free stills become `<ts>_still.tif`; survival stills `<ts>_<QUADRANT>.tif` or
`<ts>_still.tif`. When a spatial calibration is active, the save embeds
ImageJ-readable resolution tags (`XResolution`/`YResolution` = pixels-per-µm,
`ResolutionUnit = none`, `ImageDescription = "ImageJ=1.54f\nunit=um\n"`) so the
file opens pre-scaled in microns. The µm/px value comes from
`cam_mgr.active_um_per_px(FULL_W)` — always computed against the **full-frame
width** because per-pixel scale is invariant under cropping.

This is a real change from the last snapshot, which described `pictures/<date>/<ts>_still.jpg`.
Any code or doc that assumes JPEG stills is now stale.

### 2.3a session.json, the on-disk layout, and the camera state file

`sessions.py` writes manifests atomically (`.tmp` then `os.replace`). The schema
matches `CLAUDE.md` *for the manifest contents*: `schema_version`, `id`, `name`,
`assay_mode`, `assay_config`, `created_at`, `plates[]`. Session id =
`<YYYYMMDDTHHMMSS>_<6-char hash>`. Plate `folder_name` is a computed Pydantic
field: `f"{condition_id}_{name}_plate{plate_number:02d}"`. `add_plate` accepts
`replicates` (clamped 1–50) and creates that many sequential plate numbers in one
call, rejecting `(condition_id, name, plate_number)` collisions with 409.

**The directory names differ from `CLAUDE.md`.** `config.py` defines:

```
experiments/   (EXPERIMENTS_DIR)   ← session data
pictures/      (PICTURES_DIR)      ← free stills
videos/        (VIDEOS_DIR)        ← free videos
```

So the real tree under `DATA_ROOT` (`/home/pi/celegans-data`) is:

```
/home/pi/celegans-data/
├── experiments/<session_id>/session.json
│                          └── plates/<condition_id>_<name>_plateNN/   ← frames (.tif / .mp4)
├── pictures/<YYYY-MM-DD>/<ts>_still.tif      (+ .sha256, .acked, .thumbs/)
├── videos/<YYYY-MM-DD>/<ts>_video.mp4
├── flatfield/master_flat.npy                 (see §5; capture.py CLI writes elsewhere by default)
├── camera_settings.json                      (NEW — ev_bias + calibrations, see below)
├── .trash/…                                  (mirrors the source rel-path)
├── .retention-last-run                       (touch marker)
```

`CLAUDE.md`'s data-layout section still calls these `sessions/` and
`freecapture/` — **stale**. The launcher and retention daemon both hardcode the
real names (`experiments`/`pictures`/`videos`) with "must match config.py"
comments, so those three copies agree with each other but not with the doc.

**New file: `<DATA_ROOT>/camera_settings.json`.** Written atomically by
`CameraManager._save_state`, it holds `ev_bias` (float), `calibrations` (a list
of `{label, fov_cm, created_at}`), and `active_calibration` (a label or null). It
is loaded on camera start and is tolerant of a missing/malformed file. It is not
mentioned anywhere in `CLAUDE.md`.

Every saved data file gets a `<name>.sha256` sidecar at capture time. Thumbnails
are cached under a per-directory `.thumbs/` (400 px long edge; video thumbs are
the first frame via ffmpeg). `.sha256`, `.acked`, and anything under `.thumbs/`
are excluded from listings, manifests, retention, and unsynced counts.

### 2.4 Camera concurrency model (`camera.py`)

One global `CameraManager` (module-level singleton `camera_manager`), started in
the FastAPI lifespan. There is exactly one `Picamera2` instance.

Two locks / one thread:

- `_capture_lock` (`threading.Lock`) serialises all state-changing main-stream operations: `capture_still`, `start_video_recording`, `stop_video_recording`, `lock_ae`, `unlock_ae`, **and `set_ev_bias`**.
- `_frame_lock` guards the two shared preview buffers (`_latest_jpeg`, `_latest_lores`).
- A daemon **preview thread** (`_preview_loop`) continuously pulls `capture_array("lores")`, converts YUV420 (I420) → BGR, JPEG-encodes at quality 70, and stashes both the JPEG and an RGB copy.

The deliberate design choice — defended in a long comment (`camera.py:93`) — is
that the preview loop **does not** take `_capture_lock`. The lores stream is
treated as an independent passive consumer; holding the lock in the preview loop
would block `start_video_recording` from acquiring it, and `start_recording` is
what "kicks" picamera2 back into delivering lores frames after a recording stops.
This is the crux of the known concurrency history.

The camera starts in a **full-res video configuration** (4056×3040 main +
1280×960 lores), not a still configuration — despite the `start()` comment saying
"Start in still mode (full resolution)". Functionally it behaves as a still
source: `capture_still()` grabs a full-res `main` frame. Recording flips the
config to 2028×1520 @ 30 fps (`VIDEO_FRAME_US` pins the frame duration), runs an
`H264Encoder` to a `FileOutput`, then on stop flips back. Every `configure()`
resets controls, so `_apply_still_controls` re-applies both
`FrameDurationLimits = (1000 µs, MAX_AUTO_SHUTTER_US)` **and the persisted
`ExposureValue` EV bias** after each return to still mode. The video config also
re-applies the EV bias in its `controls=` block.

`capture_still()` returns `arr[..., ::-1].copy()` — a BGR→RGB channel swap, with
the comment "libcamera delivers BGR despite RGB888 label on Pi 5". **The
standalone `capture.py` (used to build the flat field) does *not* do this swap**,
so the flat-field reference and the live service stills have opposite channel
order (subtle in practice because the flat is per-channel normalised — see §6).

`capture_still()` raises `HTTPException(409)` if a recording is in progress.
Heavy timing is logged at DEBUG throughout.

**EV bias / calibration internals.** `set_ev_bias(v)` clamps to `[-3, 3]`, sets
the `ExposureValue` control, persists, and returns the clamped value. Default
`_ev_bias = -1.0` ("one stop darker than native AE"), overridden by the state
file. `active_um_per_px(width_px)` returns `fov_cm * 10000 / width_px` for the
active calibration, or `None` if uncalibrated — the FoV-width-in-cm form is
resolution-independent.

### 2.5 Retention daemon (`retention.py`)

Unchanged from the last snapshot. Standalone (`python -m capture.retention`),
driven by `celegans-retention.timer` (2 min after boot, then every 15 min). It
re-derives `DATA_ROOT` and the dir names from env vars (with a "must match
config.py" comment). Knobs (env, defaults): `MIN_FREE_GB=5`, `TARGET_FREE_GB=10`,
`MAX_AGE_DAYS=30`, `TRASH_MAX_AGE_DAYS=7`.

Reclamation **permanently deletes** files (no longer moves them to `.trash`,
which sits on the same card and freed nothing). Every deleted file is either acked
(verified copy on the laptop) or already user-trashed, so deletion is safe.
`_delete_file` removes the data file plus its `.sha256`, `.acked`, and thumbnail.

Per run: (1) `purge_expired_trash` deletes anything under `.trash/` older than
`TRASH_MAX_AGE_DAYS` (trashed files have their mtime stamped to deletion time, so
they age from deletion); (2) measure disk free, exit early if free ≥ `MIN_FREE_GB`
and no acked file exceeds `MAX_AGE_DAYS`; (3) otherwise `reclaim(max(TARGET, MIN))`
deletes oldest-first within tiers — tier 0 acked files past `MAX_AGE_DAYS`, tier 1
remaining `.trash`, tier 2 remaining acked files oldest-first — stopping once
actual disk free reaches the target. `--dry-run`/`--verbose` supported; a
`.retention-last-run` marker is touched on every non-dry run.

A separate **capture-time guard** (`app/disk_guard.py`, `ensure_capture_space`)
runs at the top of every capture endpoint: if free disk is below
`CELEGANS_CAPTURE_MIN_FREE_GB` (default 2.0) it calls `reclaim` once, and if still
below, refuses with **HTTP 507**.

---

## 3. Windows launcher (`launcher/`)

A Tkinter desktop app. `main.py` wires up three background threads — `SyncAgent`,
`MotilityAgent`, `CrawlingAgent` — each paired with a thread-safe status object,
and hands all six to `MainWindow`. (The Review feature spawns its own short-lived
worker thread on demand; it is not a long-running agent.)

### 3.1 Thread model

Every agent follows the same contract, stated in module docstrings: the **worker
thread only** writes via `status.update()` / `mark_completed()`; the **UI thread
only** reads via `status.snapshot()` / `pop_completed()`. No widget is touched off
the main thread. The UI polls on `root.after()` timers (`_POLL_MS = 2000` for the
main window, `_PROGRESS_POLL_MS = 200` for the progress dialogs). Each status
object holds a `threading.Lock` held only for a brief field copy.

### 3.2 Config (`config.py`)

A `Settings` dataclass persisted to `%APPDATA%\WormScan\config.json`; logs go to
`launcher.log` (rotating, 1 MB ×3). Fields and defaults:

- `pi_url = "http://192.168.50.2:8000"`, `token = ""`, `mirror_root = ~/Documents/WormScan`, `poll_interval_s = 120`.
- Analysis: `tierpsy_image = "tierpsy/tierpsy-tracker"`, `tierpsy_image_tag = "latest"`, `docker_command = "docker"`, `analysis_video_timeout_s = 600`, `motility_long_threshold_s = 5.0`.
- **`crawling_min_track_s = 30`** — **now a real field that persists** (previously the spinbox value was read via `getattr` and never saved; that bug is fixed).
- **`review_type = "auto"`, `review_loop_s = 3.0`** — last-used Review (grid-viewer) content type and loop length.
- **`concurrent_videos = "auto"`** — how many videos to analyse in parallel; `"auto"` derives the count from `docker info`, an int overrides (see §4.0).

`load()` filters unknown keys against the dataclass fields, so a stale config file
won't crash the app.

### 3.3 Sync flow (`sync.py`)

Unchanged in shape. `SyncAgent` is a daemon thread. On start it cleans up leftover
`.partial` files, does a one-shot clock sync, then loops: every `poll_interval_s`
(or when woken by "Sync now") it `GET /manifest` and, for every non-acked entry in
`pictures`, `videos`, and each session, it:

1. Computes the local mirror path. If the file already exists locally with a matching SHA256, skip the download.
2. Otherwise downloads to `<dest>.<sha256[:8]>.partial`, streaming and hashing as it goes; verifies the full hash; `os.replace` into place (atomic on Windows). Mismatch → discard, retry next tick.
3. `POST .../ack` with `{relative_path, sha256}`.

`_cleanup_partials` deletes orphan and already-completed partials alike on
restart, never touching a good target. `_build_name_maps` turns Pi metadata into
friendly, collision-safe folder names: sessions mirror to
`mirror/experiments/<experiment_name>/<condition_dir>/<plate NN>/<filename>`,
where `condition_dir` comes from the manifest's `condition_name`; sanitised-name
collisions get a ` (<id[:6]>)` suffix. Pictures/videos mirror to
`mirror/pictures/<date>/…` and `mirror/videos/<date>/…`. Entries missing friendly
metadata fall back to `experiments/<session_id>/<rel_path>`.

`SyncStatus` carries a colour (`green`/`yellow`/`red`/`gray`), label, last-sync
time, cumulative files/bytes, and an ephemeral 5 s clock-sync message.

### 3.4 UI structure (`ui.py`)

`MainWindow` shows a status dot + label and these buttons: Open Imaging UI, Open
Analysis, **Review (Grid Viewer)**, Open Mirror Folder, Sync now, Shut down Pi,
and Settings. "Open Imaging UI" opens `{pi_url}/?token={token}`. The status dot
prioritises a running analysis over sync state; clock-sync messages briefly
override the sync label. "Sync now" locks out until the next green sync.

`SettingsDialog` edits pi_url/token/mirror/poll-interval (poll must be ≥10). It
does **not** expose the analysis fields (threshold, min-track, concurrency,
review) — those are set from their respective dialogs or only via the JSON file.

`AnalysisDialog` launches analysis. Radio buttons select Motility, Crawling, or
Counting (Counting is permanently `disabled`, "Not yet built" tooltip). It has a
folder picker (defaults to `mirror_root`), a motility-only "Min fragment length
(s)" spinbox (1.0–30.0, hidden when Crawling is selected), a "Clear cache before
run" checkbox, and two mutually exclusive render-option frames:

- Motility renders: Tracked, Curvature, Side-by-side, Per-worm curvature traces.
- Crawling renders: Tracked, Side-by-side, Path traces, plus a "Min track span (s)" spinbox (1–600, default from `crawling_min_track_s`).

On Start it validates inputs, runs `run_preflight` (Docker installed/running,
Tierpsy image present, ffmpeg+ffprobe present, ≥1 mp4 found), **persists the
mode-appropriate setting symmetrically** (motility → `motility_long_threshold_s`,
crawling → `crawling_min_track_s`), opens a modeless `AnalysisProgressDialog`, and
calls the chosen agent's `start_analysis`. The progress dialog drives a
determinate bar by `current_index/total`, shows the current stage, and rotates
joke "flavour" strings. Cancelling sets the agent's cancel event.

Two carry-over rough edges remain in `_start`:
- The `threshold_s` (Min fragment length) spinbox is **always validated**, even in crawling mode where it is inert, and crawling's `start_analysis` is still *passed* `threshold_s=…` (it now feeds the crawling `is_long` column — see §4b — but not the quality gate).
- The progress/labelling code treats both pipelines identically; this is fine because the two status objects share a structurally identical interface.

`ReviewDialog` / `_ReviewProgressDialog` / `_ReviewStatus` (all new) implement the
"Review (Grid Viewer)" feature — see §3.6.

**Settings propagation is now symmetric.** `_on_settings_saved` calls
`update_settings` on **all three** agents (sync, motility, crawling) — the prior
bug where the crawling agent kept stale settings until restart is fixed
(`ui.py:1187–1193`, log line "Settings update propagated to: sync, motility,
crawling").

### 3.5 setup.bat / venv

`setup.bat` lives in `launcher/`, resolves repo root as its parent, is
additive/idempotent, needs no admin: checks Python ≥3.11 on PATH, creates
`launcher/.venv` if absent, `pip install -r launcher/requirements.txt`, and writes
a Desktop `WormScan.lnk` targeting `launcher/.venv/Scripts/pythonw.exe
"launcher/main.py"` with WorkingDirectory = repo root and the `wormscan.ico` icon.
Dev launch is `source launcher/.venv/Scripts/activate && python launcher/main.py`.

`launcher/requirements.txt`: requests, pandas, matplotlib, tables, h5py, numpy,
openpyxl, opencv-python, imageio-ffmpeg, scipy. (`CLAUDE.md` claims the launcher
needs "requests (only)" — **stale**.)

### 3.6 Review (Grid Viewer) feature (`ui.py` + `launcher/viewers/`)

New since the last snapshot. The "Review (Grid Viewer)" button opens
`ReviewDialog`: pick one or more day folders, choose content type (Pictures /
Videos / Auto-detect), and for videos a loop length (1–10 s). Auto-detect scans
each folder's condition subdirectories and classifies by file majority (video vs
image extension counts), cached per folder. On Start it runs the matching
standalone generator as a subprocess on a worker thread, while the UI thread polls
a lock-guarded `_ReviewStatus` through `_ReviewProgressDialog` (indeterminate
bar). When the generator exits 0, the UI parses the `Wrote <path>` stdout line
(falling back to `_review_output_path`, which re-derives the generator's default
output name from the day-prefix sort) and opens the HTML in a browser tab. Cancel
terminates the child so it is never orphaned. Last-used `review_type`/`review_loop_s`
are persisted.

The generators (`launcher/viewers/make_image_viewer.py`,
`make_video_viewer.py`) are self-contained CLIs. Both expect day folders of
`"<strain> <dose><unit>"` condition subfolders (unit `J` → J/m², `uM`/`µM` → µM),
each with a `plateNN` subfolder holding one image / mp4 (or the file directly
inside); folders starting with `_` are ignored. The image viewer caches 480 px
JPEG thumbnails in `.viewer_cache/` and builds a strain×dose grid with
loupe/click-to-pin compare, arrow-key day switching for multi-day input. The video
viewer picks the highest-numbered plate per condition, pre-transcodes its clip to
a short (~3 s by default, `--target-seconds`) downscaled (480 px, CRF 26) looping
clip in `.viewer_cache/`, and builds the same grid of looping clips. Output HTML
lands next to the first day folder (`<stem>_viewer.html` /
`<stem>_video_viewer.html`, or `…__Ndays…` for multiple folders).

---

## 4. Motility pipeline — MP4 to Excel, step by step

Entry point: `MotilityAgent._run_analysis` (`launcher/analysis/motility.py`).
Runs on the motility worker thread. Outputs land in
`<selected_folder>/_analysis_<YYYY-MM-DD_HHMMSS>/`.

### 4.0 Videos are now processed in parallel

Both pipelines parallelise across videos (commit "Parallelize motility and
crawling analysis…"). `_run_analysis` resolves a worker count via
`analysis.concurrency.resolve_workers(settings.concurrent_videos, docker_command)`
and submits each video to a `ThreadPoolExecutor`. The per-video function
(`_process_one_video_motility`) runs end-to-end on a worker thread, writing only
to its own cache dir and per-video output files, and **buffers its log lines into
a `logbuf`** that the collecting thread flushes contiguously (in completion order)
so interleaved container output stays attributable. Data rows are then accumulated
in **original discovery order** so the Excel/CSV output is byte-identical to a
serial run.

`concurrency.py` details:
- `docker_resources` runs `docker info --format "{{.NCPU}} {{.MemTotal}}"`; on any failure returns a conservative fallback `(2 cpu, 4.0 GB)`.
- `auto_workers` / `resolve_workers`: `workers = max(1, min(cpus // 2, (mem_gb − 1.5) // 2, 8))`. `"auto"` (or any non-int) uses that formula; an int is clamped to `[1, 8]`. `_MAX_WORKERS = 8`. Each Tierpsy container is single-process and peaks ~1–2 GB, hence the ~2 GB/worker budget and ~1.5 GB headroom.
- `ffmpeg_threads_per_worker(workers) = max(1, host_cpus // workers)` caps each worker's ffmpeg thread count to avoid oversubscription at the transcode stage. MJPEG (`-q:v 3`) is intra-frame, so this does not change pixel output.

A thread-safety consequence: `analysis_csv._displacement_px` now draws its random
subsample from a **per-call `np.random.default_rng()`** rather than the global RNG,
so concurrent workers don't race the shared global state.

### 4.1 Discovery and per-video loop

`find_videos` (`ffmpeg_utils.py`) walks the chosen folder up to 3 levels deep,
collecting `*.mp4`, **skipping any directory whose name starts with `_` or `.`**
(so `_analysis_*`, `_crawling_analysis_*`, `_wormscan_cache`, `.viewer_cache`,
`.trash`, `.thumbs`, `.git` are all ignored). Folder depth maps to labels via
`_resolve_video_path`: a video in the root → `condition="default", plate=<stem>`;
one level down → `condition="default", plate=<parent>`; two+ levels →
`condition=<grandparent>, plate=<parent>`.

If "Clear cache" is set, every `_wormscan_cache` dir under the folder is
`rmtree`'d first. Each video gets a cache dir at
`<video_parent>/_wormscan_cache/<stem>/`.

### 4.2 Probe + transcode (ffmpeg)

- `probe_fps` runs `ffprobe … stream=r_frame_rate`, parsed through `fractions.Fraction` → float.
- `probe_duration` runs `ffprobe … format=duration`.
- `convert_to_avi` transcodes the MP4 to an MJPEG AVI at quality 3 (`ffmpeg -y -i <mp4> -vcodec mjpeg -q:v 3 [-threads N] <avi>`), skipped if the AVI exists. The optional `-threads N` is the per-worker cap from §4.0. All ffmpeg/ffprobe calls pass `creationflags=CREATE_NO_WINDOW` on Windows.

### 4.3 Caching

A cache "hit" means `<cache>/Results/<stem>_featuresN.hdf5` exists and contains
`/trajectories_data`. On a hit, Tierpsy is skipped; the AVI is only (re)generated
if a render was requested and the AVI is missing. On a miss, the pipeline
transcodes, writes the per-video params JSON, and runs Tierpsy.

### 4.4 The Tierpsy Docker invocation

`run_tierpsy` (`docker_utils.py`). `tierpsy_process` is batch-oriented (it scans a
directory for a filename pattern), so the call mounts the video's **parent cache
dir** as `/data` and passes the AVI basename as the include pattern:

```
docker run --rm -v <cache_dir>:/data <image>
  tierpsy_process
  --video_dir_root   /data
  --mask_dir_root    /data/MaskedVideos
  --results_dir_root /data/Results
  --pattern_include  <stem>.avi
  --json_file        /data/<stem>.json
  --max_num_process  1
```

`<image>` = `{tierpsy_image}:{tierpsy_image_tag}` = `tierpsy/tierpsy-tracker:latest`
by default. Motility's timeout is `settings.analysis_video_timeout_s` (600 s). On
timeout or non-zero exit → `RuntimeError` (last 1000 chars of stderr). Tierpsy
writes `MaskedVideos/<stem>.hdf5`, `Results/<stem>_featuresN.hdf5`,
`<stem>_skeletons.hdf5`, etc. into the mounted cache dir.

### 4.5 Tierpsy parameters we override (`motility_params.json`)

The whole JSON is written per-video (`expected_fps` patched to the probed fps;
the WormScan-only key `head_angle_prominence` stripped before Tierpsy sees it).
Current values (motility):

```
analysis_type            BASE
analysis_checkpoints     COMPRESS, TRAJ_CREATE, TRAJ_JOIN, SKE_INIT, BLOB_FEATS,
                         SKE_CREATE, SKE_FILT, SKE_ORIENT, INT_PROFILE,
                         INT_SKE_ORIENT, FEAT_INIT, FEAT_TIERPSY
mask_min_area            50
mask_max_area            100000000
thresh_C                 10
thresh_block_size        61
dilation_size            5
keep_border_data         false
is_light_background      false        ← dark worms on light background
is_extract_timestamp     true
expected_fps             30.0         ← overwritten per-video with probed fps
microns_per_pixel        -1.0         ← uncalibrated; all distance metrics in pixels
is_full_bgnd_subtraction false
worm_bw_thresh_factor    1.05
strel_size               5
traj_min_area            25
traj_min_box_width       5
traj_max_allowed_dist    100
traj_max_frames_gap      10
traj_area_ratio_lim      2
roi_size                 -1
w_num_segments           24
w_head_angle_thresh      60
resampling_N             49           ← skeletons are 49 points; post-processing assumes this
filt_bad_seg_thresh      0.1
filt_max_width_ratio     2.25
filt_max_area_ratio      6
filt_min_displacement    0
filt_critical_alpha      0.01
int_avg_width_frac       0.3
int_width_resampling     15
int_length_resampling    131
head_tail_int_method     MEDIAN_INT
split_traj_time          90
ventral_side             ""
feat_skel_smooth_window  5
feat_coords_smooth_window_s 0.25
feat_gap_to_interp_s     0.25
feat_derivate_delta_time 0.33
n_cores_used             1
nn_filter_to_use         none         ← classic CV pipeline, no neural net
use_nn_food_cnt          false
MWP_*                    multi-well-plate keys, effectively off (n_wells "-1")
head_angle_prominence    0.50         ← WormScan-only; NOT sent to Tierpsy
```

`head_angle_prominence` is the one key our code consumes and Tierpsy never sees
(`_WORMSCAN_ONLY_KEYS` is popped before the JSON is written).
**`microns_per_pixel = -1.0` means motility outputs are in pixels and px/s**,
despite column names that don't say "px".

### 4.6 crawling_params.json differs — and now on more knobs

`crawling.py`'s module docstring still calls crawling "a near-exact copy … same
Tierpsy parameters … same output format". That is **false on all three counts**
now: the params differ, the grouping engine is different (§4b), and the output
schema is different. The param divergence:

| key | motility | crawling |
|-----|----------|----------|
| `mask_min_area` | 50 | 500 |
| `thresh_C` | 10 | 5 |
| `thresh_block_size` | 61 | 31 |
| `worm_bw_thresh_factor` | 1.05 | **0.92** |
| `traj_min_area` | 25 | 500 |
| `traj_max_allowed_dist` | 100 | 30 |
| `traj_max_frames_gap` | 10 | **25** |
| `filt_min_displacement` | 0 | 100 |

(`worm_bw_thresh_factor` was 1.0 at the last snapshot and is now 0.92 — a sweep
optimum; `traj_max_frames_gap` was not previously divergent.) Crawling is tuned
for fewer, larger, well-separated, slow crawlers; motility for many small/curling
ones. Both `head_angle_prominence` values are 0.50.

### 4.7 The bend-rate algorithm (as actually implemented)

Still the most mis-documented part. **Bends are counted from a head-swing angle,
not from `curvature_midbody`.** The core lives in `analysis_csv.py`.

**Signal construction (`compute_head_angle_signal`):** for each frame of a worm
track, look up its skeleton (49×2, indexed by `skeleton_id` into
`/coordinates/skeletons`). Compute `head_vec = skel[0] − skel[5]` and
`body_vec = skel[20] − skel[30]`, take the signed angle via `atan2(cross, dot)`
(radians). Missing/non-finite skeletons → NaN. Tracks with fewer than 10 valid
frames are dropped.

**Detrend (`_detrend`):** smooth the angle series with a centred rolling mean over
a 0.3 s window, then subtract a slow baseline = a centred rolling mean over a 2.0 s
window. (This 2 s baseline is the "2-second rolling mean" referenced in the
flavour text — it's applied to head angle, not curvature.)

**Peak detection:** `scipy.signal.find_peaks` on the detrended signal (positive)
and its negation (negative), both with `prominence = head_angle_prominence`. **The
effective prominence is 0.50 rad**, read from the JSON. The *function-signature
defaults* of `head_angle_prominence` are still `0.30` throughout
(`compute_head_angle_signal`, `read_fragments`, `compute_crawling_metrics`,
plot/render helpers) — those defaults are dead; the real value flows from the JSON
via `motility.py:576` / `crawling.py:705`. Don't believe the `0.30`.

**Bends → BPM:** each peak is a half-bend; `bends = (n_pos + n_neg)/2`;
`BPM = bends / duration_min`. The denominator differs by group type:
- **Curl groups** (`_metrics_curl`): bends summed over each clean sub-track; `duration_min = total_clean_s / 60` (summed clean sub-track seconds).
- **Collision sub-tracks** (`_metrics_one_collision_subtrack`): `bends_per_minute(sig)`, whose denominator is `signal["n_valid"]/fps/60` — valid-angle frames in that sub-track.

So curl and collision BPM use subtly different denominators. `bend_interval_cv` =
CV of inter-peak intervals (seconds); NaN with <3 peaks.

The block is still headed by a comment *"Bend counter — UNCHANGED from v1 … Do not
modify."* yet every row is stamped `bend_method = "head_angle_peaks_v2"`. The
comment and the label disagree; the label is what's written to output.

### 4.8 Fragment grouping, flicker filter, debris filter (motility only)

Unchanged. Tierpsy fragments a worm's track on self-touch (curl) or collision; the
pipeline reassembles before scoring.

**Grouping (`fragment_grouping.py`).** One `FragmentInfo` per `worm_index_joined`.
Two fragments are adjacent if the later starts within
`TIME_GAP_THRESHOLD_SECONDS = 5.0` of the earlier ending **and** their end→start
centroid distance ≤ `DISTANCE_THRESHOLD_PIXELS = 50`. Union-find → components. A
component is **curl** if every node has ≤1 in- and ≤1 out-edge (a linear chain),
else **collision** (branching). Solo fragments are size-1 curl groups.

**Flicker filter (`flicker_filter.py`).** Per track, per-frame skeleton length;
centred rolling std over `FLICKER_WINDOW_SECONDS = 0.5`; a frame is flicker if the
skeleton is missing or the rolling std exceeds `FLICKER_STD_THRESHOLD_PIXELS = 20`.
The track splits into clean sub-tracks at the flagged boundaries.

**Per-group (`read_fragments`).** Curl: if total clean time <
`MIN_OBSERVATION_TIME_SECONDS = 10.0`, drop (`curl_too_short`, or
`flicker_killed_track` if zero clean frames); else curl metrics. Collision:
estimate max concurrently-active clean sub-tracks, cap at
`COLLISION_WORM_COUNT_CAP = 3`, select that many overlapping sub-tracks maximising
frames, emit one row per selected sub-track clearing the 10 s minimum.

**Debris filter** (after expansion): drop a row if either fires —
Rule 1 (stationary): `displacement_px < 8.0` and `bpm < 5.0`; Rule 2 (flickery
blob): `length_cv > 0.10` and `solidity_median > 0.6` and `speed_median_abs < 10.0`
(all three finite and passing). Shape metrics come from `timeseries_data`
(length, speed) and `blob_features` (solidity), best-effort.

Per-video decisions are logged to `per_video/<condition>__<plate>_analysis_log.json`.

### 4.9 Per-worm row schema and the `is_long` gate (motility)

Each surviving worm row carries (among others): `condition`, `plate`, `worm_index`
(post-filter sequential), `repr_tierpsy_id`, `member_tierpsy_ids`, `frames`,
`duration_s`, `bpm`, `bend_interval_cv`, `is_long`, `coverage_pct`,
`is_full_track`, `group_classification`, `curl_count`, `fragment_count`,
`valid_frac`, `displacement_px`, `length_cv`, `solidity_median`,
`speed_median_abs`, `group_id`, `bend_method`.

- Curl `duration_s` = wall-clock span; `valid_frac` = clean time / span.
- Collision `duration_s` = sub-track length / fps; `valid_frac` = 1.0.
- `is_long = duration_s >= long_threshold_s` (default 5 s). **Only long worms feed the summary statistics.**
- `coverage_pct` = clean frames / total video frames × 100; `is_full_track = coverage_pct >= 90`. **`is_full_track` is computed but never read and never exported** — dead.

### 4.10 Outputs (motility)

Written to `_analysis_<timestamp>/`:

- **`motility_results.xlsx`** — one sheet per condition (sanitised+deduped, ≤31 chars), rows sorted by `duration_s` descending, fixed columns: `plate, worm_index, repr_tierpsy_id, group_id, frames, duration_s, bpm, bend_interval_cv, is_long, fps_used, group_classification, curl_count, fragment_count, valid_frac, displacement_px, coverage_pct, length_cv, solidity_median, speed_median_abs`. Plus a `_summary` sheet. `is_full_track`, `member_tierpsy_ids`, `bend_method` exist in the row dicts but are **not** in the exported column list.
- **`motility_summary.csv`** — the `_summary` rows. Per video: total/long fragment counts, `bpm_{median,mean,std,min,max}_long`, `bend_cv_{mean,median}_long`, median `length_cv`/`solidity`/`speed` over long worms, curl/collision counts among long worms, mean valid-frac & fragment count, `fps_used`, `duration_video_s`, `status`.
- **`overview.png`** — box-and-whisker of median-BPM-per-video by condition (or a per-plate bar chart with one condition).
- **`per_video/<condition>__<plate>.png`** — sorted BPM bar chart of long fragments + detrended head-angle traces for the 3 longest, peaks marked, y-axis "Head angle (rad)".
- **`per_video/<…>_analysis_log.json`**, **`log.txt`**.

**Optional renders** (`render_video.py`), AVI required:
- `_tracked.mp4` — skeleton polylines coloured/labelled by stable `worm_index` (12-colour palette persisting across a worm's fragments); filtered-out fragments get a faint grey centroid dot only.
- `_curvature.mp4` — skeleton coloured by the **sign of detrended `curvature_midbody`** (red +, blue −, grey 0). The *only* place `curvature_midbody` is used; same 0.3 s/2.0 s windows but a different signal from the BPM path.
- `_sidebyside.mp4` — original beside masked+tracked.
- `<…>_traces/worm_<id>.png` + `.mp4` — per long worm: head-angle trace PNG + two-panel MP4. The loop iterates over `is_long` rows (the variable is named `full_track_rows` but filters on `is_long`, not `is_full_track`).

`render_video.py` now also contains the **velocity-arrow overlay** machinery
(`_prepare_arrow_worms`, `_draw_arrows_and_markers`, `ARROW_RENDER_SCALE = 45.0`,
reversal/turn markers) — used by the *crawling* renders (§4b), not by motility,
but it lives in this shared module.

### 4.11 Worm-index → render mapping (motility)

Before rendering, `motility.py` builds `worm_index_map: {tierpsy_id → worm_index}`
from each kept row's `member_tierpsy_ids` (or `repr_tierpsy_id`). Renders key on
`worm_index_joined` (the `_skeletons.hdf5` trajectory id), so this map lets a
render colour/label by the grouped stable worm number that appears in the Excel.
Renders read `_skeletons.hdf5` (49-point `/skeleton` + `trajectories_data`) and
`MaskedVideos/<stem>.hdf5` (`/mask`), one frame at a time, piping raw BGR24 into
`ffmpeg -vcodec libx264 -preset fast -crf 22 -pix_fmt yuv420p`.

---

## 4b. Crawling pipeline (`crawling.py`, `crawling_metrics.py`, `crawling_fragment_grouping.py`, `crawling_render.py`, `crawling_plots.py`)

A second pipeline launched from the same dialog. It still mirrors motility's thread
contract, cache layout, ffmpeg/AVI step, and parallel-execution scaffolding, but it
has diverged substantially. Outputs land in
`<selected_folder>/_crawling_analysis_<YYYY-MM-DD_HHMMSS>/`.

### 4b.1 Grouping: a position-based linker, NOT the motility engine

Crawling no longer calls `read_fragments`. It uses its own
`crawling_fragment_grouping.link_fragments`, a position-only nearest-neighbour
stitcher over the `*_skeletons.hdf5` `trajectories_data`:

- For each fragment (a `worm_index_joined`), processed by `f_end` ascending, find candidate successors that *start* within `(f_end, f_end + T_MAX_S*fps]` (`T_MAX_S = 5.0`), unclaimed, not already in the same group.
- Score by Euclidean distance from the fragment's end position (mean of last 3 frames) to the candidate's start position (mean of first 3). Keep within `D_MAX = 150 px`.
- The nearest candidate wins **unless ambiguous**: among candidates starting within `AMBIG_TIME_WINDOW_S = 1.0 s` of the best, if any has distance `< AMBIG_RATIO (2.0) × best`, the link is refused (the crossing stays broken) — **except** a sub-pixel noise floor: if best and runner-up are both `< AMBIG_FLOOR_PX = 3.0`, the ambiguity check is skipped.
- Greedy + union-find. Returns `(groups, ambiguity_skips)`.

It neither classifies curl/collision nor flicker-filters. A `_linker_log`
synthesises a sidecar shape compatible with what `crawling.py` writes
(`input_track_count`, `groups_formed` with curl/collision = 0, `worms_dropped` = 0,
plus `ambiguity_skips`).

### 4b.2 Per-worm metrics (`compute_crawling_metrics`)

Each linked group becomes one GROUPED worm row (`worm_index` = group id,
`member_tierpsy_ids` = `";"`-joined members, `repr_tierpsy_id` = first by start
frame, `group_classification = "linked"`). Metrics are computed on the combined
member track and **never impute across gaps**. The columns (see `PER_WORM_COLS`,
which interleaves BL companions and arrow columns next to their anchors):

- Head-angle `bpm` + `bend_interval_cv`, computed here from the concatenated member skeletons (shared `compute_head_angle_signal` / `bend_interval_cv`); `is_long` derived from `track_duration_s >= long_threshold_s` (the dialog's `threshold_s`).
- Speed kinematics: `mean_speed_pxs`, `mean_forward_speed_pxs`, `mean_backward_speed_pxs`, `fraction_forward/backward/paused`. "Paused" = `|speed|` below `_PAUSED_FRACTION_OF_MEDIAN = 0.10` × the video-wide median `|speed|`.
- Reversals: `reversal_count` + `reversal_rate_per_min` from forward→backward transitions in `motion_mode` (fallback: speed sign), tolerating brief **measured** pauses up to `REVERSAL_PAUSE_TOLERANCE_FRAMES = 60` (2 s @ 30 fps) but never a data gap (NaN). Reversal rate is per **observed** minute (finite-speed frames), not span.
- Path geometry over the combined centroid track: `path_length_px` (frame-adjacent steps only), `net_displacement_px`, `tortuosity = path/max(net,1)`.
- `mean_length_px`, `mean_width_midbody_px`, `track_duration_s` (group frame span), `longest_continuous_run_s`, `skeleton_coverage`.
- Activity / variability: `mean_speed_when_moving`, `activity_fraction_above_{1,3,5}pxs`, `speed_cv`, `length_cv`.
- **Body-length-normalized companions** (`BL_COLS`): each pixel metric ÷ a single per-video `plate_mean_length_px` scalar (the trimmed-mean worm length on that plate — `BL_CALIB_*` constants), so cross-plate / cross-day magnification drift cancels while individual size variation is retained. Includes `mean_speed_bls`, forward/backward BL speeds, `path_length_bl`, `net_displacement_bl`, `mean_speed_when_moving_bls`, and `activity_fraction_above_{0p05,0p10,0p20}_bls` (BL/s breakpoints). All NaN if the plate scalar can't be computed.
- **Velocity-arrow reversal/turn columns** (`ARROW_COLS`): `arrow_reversal_count`, `arrow_reversal_rate_per_min`, `turn_count`, `turn_rate_per_min`, from `_velocity_arrow_events` — a motion_mode-independent detector on the dense centroid velocity (centered finite difference, heading change `LOOKAHEAD` frames before vs after, NMS; `ARROW_REVERSAL_THRESHOLD_DEG = 140`, `ARROW_TURN_THRESHOLD_DEG = 60`, BL-relative min speed). Per the comment these live **alongside** the motion_mode `reversal_count` "for at least one analysis cycle of side-by-side comparison".
- `passed_filter` (the quality gate, below).

`longest_continuous_run_s` is the longest skeletonised run within a single member
fragment, bridging internal skeleton-fitter hiccups of `≤ LONGEST_RUN_BRIDGE_FRAMES
= 30` (1 s @ 30 fps). It is now an **information column only** — no longer the
quality gate.

`compute_crawling_metrics` runs in two passes: the first builds rows and the dense
centroid arrays; the second computes `plate_mean_length_px` and fills the BL
columns and the velocity-arrow events (which need the BL-relative speed cutoff).
Several keys (`reversal_frames`, `arrow_f0/x/y/vx/vy`, `arrow_*_event_frames`) are
**renderer-only** and intentionally dropped by the `PER_WORM_COLS` projection.

### 4b.3 Quality gate (`_passes_filter`)

A worm passes if `track_duration_s >= min_span_s` **and** `skeleton_coverage >=
SKELETON_COVERAGE_MIN = 0.70`. `min_span_s` is the dialog's "Min track span (s)"
(default 30, persisted as `crawling_min_track_s`); the module fallback is
`MIN_SPAN_S = 30.0`. This **replaces** the old "longest unbroken run ≥ N" gate and
the old coverage floor of 0.30. Raw per-worm rows are always written; the gate is
recomputed at aggregation time, so thresholds can be re-tuned on a saved `per_worm`
table without re-running Tierpsy. (The per-video log line says "coverage >= 70%".)

### 4b.4 Tierpsy invocation + timeout

Tierpsy runs through `_run_tierpsy_instrumented` (crawling-only): it streams docker
stdout+stderr line-by-line to the console (tagged with the video stem so parallel
containers stay attributable), prints the exact command, and dumps a recursive
listing of the output dir on exit. Its timeout is **hardcoded to
`_TIERPSY_TIMEOUT_S = 3600`** and passed explicitly — it ignores
`settings.analysis_video_timeout_s` (which motility honours at 600 s).

### 4b.5 Aggregation + outputs

`aggregate_per_condition` drops worms failing `_passes_filter`, then per condition
reports `n_worms_total`, `n_worms_kept`, and mean/median/std of each `AGG_COLS`
metric (BPM, bend CV, the kinematics) plus a **median-only** column for each
`ACTIVITY_COLS`, `BL_COLS`, and `ARROW_COLS` metric.

Outputs in `_crawling_analysis_<timestamp>/`:
- **`crawling_results.xlsx`** — `per_worm` (all rows, `PER_WORM_COLS`, rounded 4dp) and `per_condition` sheets.
- **`crawling_summary.csv`** — the `per_condition` aggregates.
- **`overview.png`** — **new**: a multi-panel figure (`crawling_plots.make_crawling_overview_png`), one box/bar panel per `AGG_COLS` metric by condition, kept-worms only. (The last snapshot noted crawling had *no* overview PNG — that is no longer true. There is still no per-video summary PNG.)
- **`per_video/<condition>__<plate>_analysis_log.json`** — the linker sidecar (input track count, group count, ambiguity skips).
- **`log.txt`**.
- Optional renders, restricted to filter-passing worms: `_tracked.mp4` and `_sidebyside.mp4` (shared `render_video.py`, via `worm_index_map`, with the velocity-arrow + reversal/turn-marker overlay on the tracked render), and `_path_traces.mp4` (`crawling_render.py` — a darkened video with fading per-worm centroid trails over a 10 s window, yellow reversal-flash rings, and velocity arrows only, no event markers).

`crawling.py` still accepts and forwards `threshold_s`; it now feeds the per-worm
`is_long` column (via `long_threshold_s`) but plays no part in the quality gate —
only `min_span_s` gates worms.

---

## 5. Configuration surface

**Pi service env vars** (`capture/.env`, prefix `CELEGANS_`, loaded by
pydantic-settings; `.env.example` is the template):

- `CELEGANS_TOKEN` (required).
- `CELEGANS_DATA_ROOT` (default `/home/pi/celegans-data`).
- `CELEGANS_EXPERIMENTS_DIR` / `_PICTURES_DIR` / `_VIDEOS_DIR` (`experiments`/`pictures`/`videos`).
- `CELEGANS_HOST` (`0.0.0.0`), `CELEGANS_PORT` (`8000`).
- `CELEGANS_MAX_AUTO_SHUTTER_US` (`500000` = 500 ms AE shutter cap).
- `CELEGANS_CAPTURE_MIN_FREE_GB` (`2.0`) — capture-time guard floor; below it (after a reclaim) captures are refused with HTTP 507.
- `CELEGANS_RETENTION_TRASH_MAX_AGE_DAYS` (`7.0`) — carried in `config.py` for validation/visibility; `retention.py` also reads it (and the other retention knobs) directly from env: `..._MIN_FREE_GB=5`, `..._TARGET_FREE_GB=10`, `..._MAX_AGE_DAYS=30`, `..._TRASH_MAX_AGE_DAYS=7`.

The systemd capture unit substitutes `${CELEGANS_HOST}`/`${CELEGANS_PORT}` into the
uvicorn `ExecStart`.

**Persistent camera state** (`<DATA_ROOT>/camera_settings.json`, not env):
`ev_bias` (default −1.0, clamp ±3), `calibrations` (list of `{label, fov_cm,
created_at}`), `active_calibration`.

**Launcher config** (`%APPDATA%\WormScan\config.json`): see §3.2. Notable new
knobs: `crawling_min_track_s` (30), `concurrent_videos` ("auto"), `review_type`
("auto"), `review_loop_s` (3.0).

**Hardcoded paths / magic constants:**

- Pi network: `192.168.50.2` (Pi), `192.168.50.1` (laptop), SSH alias `celegans`. Launcher default `pi_url` bakes in `http://192.168.50.2:8000`.
- Camera: `FULL = 4056×3040`, `VIDEO = 2028×1520 @ 30 fps`, `PREVIEW = 1280×960`, default video bitrate `9_000_000` bps, default capture duration `30 s`. EV bias default −1.0.
- Flat-field master: `master_flat.npy`, averaged from `FLAT_N_FRAMES = 16` frames.
- Skeletons `resampling_N = 49`; head-angle uses indices 0/5 (head) and 20/30 (body); flicker window 0.5 s, threshold 20 px; detrend windows 0.3 s / 2.0 s; bend prominence 0.50 rad.
- Motility pipeline (`analysis_csv.py`): distance 50 px, time gap 5 s, min observation 10 s, collision cap 3, debris displacement 8 px / bpm 5 / length_cv 0.10 / solidity 0.6 / speed 10.
- Crawling (`crawling_metrics.py`): `MIN_SPAN_S = 30`, `SKELETON_COVERAGE_MIN = 0.70`, paused = 10% of median speed, `LONGEST_RUN_BRIDGE_FRAMES = 30`, `REVERSAL_PAUSE_TOLERANCE_FRAMES = 60`, arrow thresholds 140°/60°, path-trace window 10 s, darken 0.5; linker `D_MAX = 150`, `T_MAX_S = 5`, `AMBIG_RATIO = 2`, `AMBIG_FLOOR_PX = 3`, `AMBIG_TIME_WINDOW_S = 1`.
- Concurrency (`concurrency.py`): `_MAX_WORKERS = 8`, fallback `(2 cpu, 4 GB)`, ~2 GB/worker, 1.5 GB headroom.
- Render encode: libx264, preset fast, crf 22, yuv420p; tracked/side-by-side worm-ID font scale 1.4 / thickness 3; path-trace label font scale 1.0 / thickness 2; `ARROW_RENDER_SCALE = 45.0`.

**Auth mechanism:** one shared token, `secrets.compare_digest`, header or query
param. Web UI prompts and stores it client-side; launcher persists it in
`config.json` and passes `X-Auth-Token`. Preview stream and "Open Imaging UI"
deep-link use `?token=` because they can't set headers.

---

## 6. Known deviations and rough edges

Roughly ordered by how likely they are to bite you.

1. **`CLAUDE.md` camera identity is wrong.** Hardware is IMX477 HQ Camera (4056×3040), confirmed by `capture.py` and `camera.py`. (§2.1)

2. **`CLAUDE.md` on-disk layout is wrong.** Real names are `experiments/`, `pictures/`, `videos/` (config.py), not `sessions/` + `freecapture/`. Also undocumented: `<DATA_ROOT>/camera_settings.json`. (§2.3a)

3. **Stills are TIFF now, not JPEG.** `save_still` writes LZW TIFF (`<ts>_still.tif`), optionally with ImageJ calibration tags. Any assumption of `.jpg` stills (including the prior snapshot's §2) is stale. (§2.3)

4. **Flat-field directory mismatch — likely a real bug, still present.** The service reads the master flat from `DATA_ROOT/flatfield` (`capture_ops.load_flat`), but its own error message tells you to run `python3 capture/capture.py --capture-flat`, and that CLI's default `FF_DIR` is `<repo>/data/flatfield` (`PROJECT_DIR/data/flatfield`). Following the instructions produces a flat the service can't find. Dormant because flat-field correction is opt-in per request.

5. **BGR/RGB asymmetry between service and flat reference.** `capture_still` swaps BGR→RGB; the standalone `capture.py` that builds the flat does not. Opposite channel order; mostly cosmetic because the flat is per-channel normalised. (§2.4)

6. **The bend rate is a head-angle metric, not curvature.** BPM comes from peaks in a detrended head-swing angle (skeleton points 0/5 vs 20/30), prominence 0.50 rad. `curvature_midbody` only colours the optional curvature render. (§4.7, §4.10)

7. **`head_angle_prominence` function-signature defaults (0.30) are dead.** The real value (0.50) flows from the JSON in both pipelines. (§4.7)

8. **"Bend counter UNCHANGED from v1 / Do not modify" vs `bend_method="head_angle_peaks_v2"`.** The comment and the stamped label disagree; output says v2. (§4.7)

9. **Curl vs collision BPM use different denominators.** Curl divides by total clean sub-track seconds; collision by valid-angle-frame seconds. Cross-classification BPM comparisons aren't strictly apples-to-apples. (§4.7)

10. **`crawling.py`'s module docstring is now triply false.** It claims crawling is "a near-exact copy … same Tierpsy parameters … same output format." The params diverge on eight knobs (§4.6), the grouping engine is a different module (`crawling_fragment_grouping`, not `fragment_grouping`), and the output schema (per_worm/per_condition with BL + activity + arrow columns) bears no resemblance to motility's. (§4b)

11. **`is_full_track` (motility) is computed but never used or exported.** Dead field. The per-worm-trace render filters on `is_long` despite a variable named `full_track_rows`. (§4.9, §4.10)

12. **Motility Excel drops some computed columns.** `member_tierpsy_ids`, `bend_method`, `is_full_track` exist in the row dicts but aren't in `_sheet_cols`, so they never reach `motility_results.xlsx`.

13. **Crawling Tierpsy timeout is hardcoded to 3600 s**, ignoring `settings.analysis_video_timeout_s` (which motility honours at 600 s). (§4b.4)

14. **Crawling `threshold_s` is only half-used.** The dialog passes it to the crawling agent; it now drives the `is_long` per-worm column but plays no role in the quality gate (only `min_span_s` does). The Min-fragment-length spinbox is also validated in `_start` even when Crawling is selected, where it is otherwise inert. (§3.4, §4b)

15. **Velocity-arrow reversal/turn columns are explicitly provisional.** The code comment says they "live ALONGSIDE the existing `reversal_count` for at least one analysis cycle of side-by-side comparison." Two parallel reversal metrics ship today; expect one to be retired. (§4b.2)

16. **Static file mount is CWD-relative** (`StaticFiles(directory="app/static")`, `main.py:176`). Only correct because the systemd unit sets `WorkingDirectory=.../capture`. Launch elsewhere and the web UI 404s.

17. **`microns_per_pixel = -1.0`: motility/crawling distance and speed metrics are pixels and px/s.** The new BL-normalized crawling columns divide out plate magnification but are still derived from pixel measurements; the ImageJ TIFF calibration is independent of the analysis pipeline (it scales the *image file*, not the HDF5 features).

18. **`CLAUDE.md` says the launcher needs "requests (only)".** It needs the full scientific stack (pandas, numpy, h5py, opencv, scipy, matplotlib, openpyxl, tables, imageio-ffmpeg). (§3.5)

19. **Mixed async/sync in the session router.** `delete_session`/`delete_condition`/`reorder_conditions`/`rename_condition`/`capture`/`ack` run via `asyncio.to_thread`, but `create_session` and `add_plate` run synchronously on the event loop. Fast enough not to matter, but inconsistent.

20. **`clock-sync` and `shutdown` depend on sudoers entries** for `/bin/date` and `/sbin/shutdown`; if missing the endpoint 500s.

21. **`bend_calibration.py` is now duplicated and tracked twice** — `docs/calibration/bend_calibration.py` and `launcher/bend_calibration.py` — the latter with hardcoded `C:\Users\Isabe\Documents\WormScan\…` manual-count paths.

22. **Untracked cruft in the working tree.** Root-level `check_*.py` / `inspect_filter_decisions*.py` (hardcoded `…\Desktop\Tierpsyclips\…`), most of `launcher/tools/` (only `tierpsy_param_sweep.py` is tracked), the new untracked `README.md` and `VIEWER_LAUNCHER_SPEC.md`, and two stray untracked artifacts in the repo root: a directory `_saved (previously only sync and motility were updated)` and a file `receive each settings update`. None are imported by the app. Several tracked files in `launcher/analysis/` and `launcher/viewers/` currently have **uncommitted modifications** — this snapshot reflects the working-tree contents, not `HEAD`.
