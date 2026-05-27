# CURRENT_STATE.md

**Ground-truth snapshot of how the code actually works — generated 2026-05-27.**

This describes the code as it sits in the working tree right now (including
uncommitted changes), not the design intent and not the history. Where a
comment, docstring, or `CLAUDE.md` claim no longer matches the code, it is
flagged. If you are reasoning about this system from an older conversation,
trust this document over your memory.

A blunt heads-up before anything else: **`CLAUDE.md` is wrong about the camera
and wrong about the on-disk data layout.** Details in §2 and §6. Don't anchor on
it.

---

## 1. Repo layout

Top-level, one line each:

- `capture/` — the FastAPI service that runs on the Pi (camera control, capture, file serving, manifests, acks).
- `capture/capture.py` — standalone full-res still-capture + flat-field CLI script. Marked "do not modify". The service imports two functions from it (`apply_flat_field`, `load_master_flat`); the rest is CLI-only.
- `capture/retention.py` — disk-retention daemon, run via systemd timer (`python -m capture.retention`).
- `launcher/` — the Windows Tkinter app: syncs files off the Pi and runs the local analysis pipelines.
- `launcher/analysis/` — the motility and crawling analysis pipelines (Docker/Tierpsy + post-processing).
- `launcher/tools/` — untracked ad-hoc diagnostic scripts (param sweep, clip cutter, skeleton/head-angle inspectors). Not part of the app.
- `deploy/` — three systemd units: capture service, retention oneshot service, retention timer.
- `scripts/` — bash helpers: `deploy.sh` (push→pull→restart), clock sync, data wipe, folder renamers.
- `docs/calibration/` — bend-calibration script + reference PNGs (fast/slow worm examples).
- `CLAUDE.md` — project instructions. **Stale in places** (see §6).
- `STATUS.md`, `BACKLOG.md`, `motility_analysis_spec.md` — narrative/spec docs (not covered here per scope).
- Root-level `check_*.py`, `inspect_filter_decisions*.py` — untracked one-off debugging scripts with hardcoded `C:\Users\Isabe\Desktop\Tierpsyclips\...` paths. Throwaway; ignore.

The repo holds **two** Tierpsy-parameter JSONs at `launcher/`: `motility_params.json`
and `crawling_params.json`. They differ materially (see §4.6).

---

## 2. Pi capture service (`capture/app/`)

FastAPI app, served by uvicorn (`app.main:app`). Started by systemd
(`deploy/celegans-capture.service`) with `WorkingDirectory=/home/pi/celegans-imaging/capture`
and `EnvironmentFile=.../capture/.env`. The working directory matters: the static
mount uses the **relative** path `app/static` (`main.py:143`), so the app only
serves its web UI when launched from `capture/`.

### 2.1 The camera is an IMX477 HQ Camera, not an IMX708 Module 3

`CLAUDE.md` says "Raspberry Pi Camera Module 3 (IMX708)". The code says otherwise
and is internally consistent about it:

- `capture/capture.py` docstring: "Camera: Sony IMX477 HQ Camera (12.3 MP)", sensor 4056×3040.
- `camera.py`: `FULL_W, FULL_H = 4056, 3040` (the IMX477 full array; the IMX708 is 4608×2592).

So full-res stills are 4056×3040. Treat the `CLAUDE.md` camera section as wrong.

(There is also a smaller lie inside `camera.py:19`: the comment on the video
resolution says `IMX477 mode 2: ... 53.77 fps max`, while the next constants lock
the stream to 30 fps. The 53.77 figure is informational and unused.)

### 2.2 Endpoints

Auth model: a single shared bearer token (`auth.require_token`), accepted either
as the `X-Auth-Token` header **or** a `?token=` query param, compared with
`secrets.compare_digest`. Only `/health` is unauthenticated. The MJPEG preview
re-implements the same check inline against the query param because `<img src>`
can't send headers (`preview.py:40`).

**Top-level (`main.py`):**
- `GET /health` — `{"status":"ok","schema_version":1}`, no auth.
- `GET /status` — disk free/total GB, `data_root`, `camera_ready`, `ae_locked`, unsynced-file stats, and `last_retention_run_at` (mtime of `.retention-last-run` marker). Unsynced stats are computed by walking `experiments/`, `pictures/`, `videos/` and counting files that have no sibling `.acked` and aren't sidecars/thumbs; the result is cached 30 s (`_STATUS_CACHE_TTL`).
- `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/plates`, `DELETE /sessions/{id}`, `DELETE /sessions/{id}/conditions/{condition_id}`.

**Camera control (`routers/camera_ctrl.py`, prefix `/camera`):**
- `POST /camera/ae/lock` — reads current `ExposureTime`/`AnalogueGain` from a capture request's metadata, then disables AE and pins those values. Returns the locked exposure/gain.
- `POST /camera/ae/unlock` — re-enables AE.
- `GET /camera/exposure` — current lock state + pinned values.

**Preview (`routers/preview.py`):**
- `GET /preview.mjpg?token=...` — multipart MJPEG stream of the lores frames; yields only when the JPEG changes, else sleeps 1/15 s.
- `GET /focus` — Laplacian variance of the centre 400×400 crop of the latest lores frame (`focus.compute_focus_score`). Higher = sharper.

**Free capture (`routers/free_capture.py`, prefix `/capture/free`):**
- `POST /capture/free/still` — full-res JPEG to `pictures/<YYYY-MM-DD>/<ts>_still.jpg`. Optional flat-field correction.
- `POST /capture/free/video` — H.264 recording for `duration_s`, remuxed to MP4, saved to `videos/<YYYY-MM-DD>/`.
- `GET /capture/free/files`, `GET /capture/free/videos` — list a day's stills/videos.
- `GET .../files/{date}/{filename}` and `.../videos/{date}/{filename}` — serve a file, or its thumbnail with `?thumb=1`.
- `DELETE` variants — move file to `.trash/`.

**Plate capture (`routers/plate_capture.py`):**
- `POST /sessions/{id}/plates/{plate_id}/capture` — branches on `session.assay_mode`: motility records a video (duration/bitrate from request or `assay_config`), survival captures a still (optionally per-quadrant, optionally flat-fielded).
- `GET .../files` and `.../files/{filename}` (with `?thumb=`) — list/serve plate files.
- `DELETE .../{plate_id}` and `DELETE .../files/{filename}` — trash a plate or a single file.

**Manifests + acks (`routers/manifest.py`):**
- `GET /manifest` — the big one the launcher polls: all sessions' file manifests plus the `pictures` and `videos` manifests, gathered concurrently via `asyncio.gather`.
- `GET /sessions/{id}/manifest`, `GET /capture/free/manifest`, `GET /capture/free/videos/manifest` — scoped manifests.
- `POST /sessions/{id}/files/ack`, `POST /capture/free/files/ack`, `POST /capture/free/videos/ack` — client confirms it has a verified copy; server checks the supplied SHA256 against the stored one and writes a `<file>.acked` marker. SHA256 mismatch → 409; path traversal → 400/403.

Each manifest file entry carries `relative_path`, `size_bytes`, `sha256` (read from the `.sha256` sidecar, lazily computed if missing), `mtime`, `acked`, `acked_at`. Session entries additionally carry `plate_id`, `condition_name` (formatted as `"{condition_name} {condition_id}"` via `_resolve_condition_name`), and `plate_label` (`"plate NN"`).

**System (`routers/system.py`):**
- `POST /clock-sync` — client posts its ISO time; server computes the offset, refuses if it deviates >5 years, otherwise runs `sudo -n date -s <reformatted>`. Needs a sudoers entry for `/bin/date`; the error message says so.
- `POST /shutdown` — fires `sudo /sbin/shutdown -h now` via `Popen` and returns 202.

### 2.3 session.json and the on-disk layout (as actually written)

`sessions.py` writes manifests atomically (`.tmp` then `os.replace`). The schema
matches `CLAUDE.md` *for the manifest contents*: `schema_version`, `id`, `name`,
`assay_mode`, `assay_config`, `created_at`, `plates[]`. Session id =
`<YYYYMMDDTHHMMSS>_<6-char sha256 of ts+name>`. Plate `folder_name` is a
**computed** Pydantic field: `f"{condition_id}_{name}_plate{plate_number:02d}"`
(`models.py:16`). `Plate` also has an optional `condition_name` (human label;
`None` on legacy plates) — note this is distinct from `name`.

`add_plate` accepts `replicates` (clamped 1–50) and creates that many sequential
plate numbers in one call, rejecting `(condition_id, name, plate_number)`
collisions with 409.

**Where the directory tree actually lives differs from `CLAUDE.md`.** The config
(`config.py`) defines the on-disk folder names as:

```
experiments/   (EXPERIMENTS_DIR)   ← session data
pictures/      (PICTURES_DIR)      ← free stills
videos/        (VIDEOS_DIR)        ← free videos
```

So the real tree under `DATA_ROOT` (`/home/pi/celegans-data`) is:

```
/home/pi/celegans-data/
├── experiments/<session_id>/session.json
│                          └── plates/<condition_id>_<name>_plateNN/   ← frames
├── pictures/<YYYY-MM-DD>/<ts>_still.jpg            (+ .sha256, .acked, .thumbs/)
├── videos/<YYYY-MM-DD>/<ts>_video.mp4
├── flatfield/master_flat.npy                       (see §5 — capture.py writes elsewhere by default)
├── .trash/...                                       (mirrors the source rel-path)
├── .retention-last-run                              (touch marker)
```

`CLAUDE.md`'s data-layout section still calls these `sessions/` and
`freecapture/` — **stale**. The launcher and retention daemon both hardcode the
real names (`experiments`/`pictures`/`videos`) with comments saying "must match
config.py", so the three copies are in sync with each other but not with the doc.

Every saved data file gets a `<name>.sha256` sidecar written atomically at
capture time. Thumbnails are cached under a per-directory `.thumbs/` (400 px long
edge; video thumbnails are the first frame via ffmpeg). `.sha256`, `.acked`, and
anything under `.thumbs/` are excluded from listings, manifests, retention, and
unsynced counts.

### 2.4 Camera concurrency model (`camera.py`)

One global `CameraManager` (module-level singleton `camera_manager`), started in
the FastAPI lifespan. There is exactly one `Picamera2` instance.

Two locks / one thread:

- `_capture_lock` (`threading.Lock`) serialises all **state-changing** main-stream operations: `capture_still`, `start_video_recording`, `stop_video_recording`, `lock_ae`, `unlock_ae`.
- `_frame_lock` guards the two shared preview buffers (`_latest_jpeg`, `_latest_lores`).
- A daemon **preview thread** (`_preview_loop`) continuously pulls `capture_array("lores")`, converts YUV420 (I420) → BGR, JPEG-encodes at quality 70, and stashes both the JPEG and an RGB copy.

The deliberate design choice — and there's a long comment defending it
(`camera.py:84`) — is that the preview loop **does not** take `_capture_lock`.
The lores stream is treated as an independent passive consumer; holding the lock
in the preview loop would block `start_video_recording` from acquiring it, and
`start_recording` is what "kicks" picamera2 back into delivering lores frames
after a recording stops. This is the crux of the known concurrency history
(see the project memory note on the picamera2 deadlock).

The camera starts in a **full-res video configuration** (4056×3040 main +
1280×960 lores), not a still configuration — despite the `start()` comment
saying "Start in still mode (full resolution)". Functionally it behaves as a
still source: `capture_still()` grabs a full-res `main` frame. Recording flips
the config to 2028×1520 @ 30 fps (`VIDEO_FRAME_US` pins the frame duration), runs
an `H264Encoder` to a `FileOutput`, then on stop flips back to the full-res
config. Every `configure()` resets controls, so `_apply_still_controls`
(re-applying `FrameDurationLimits = (1000 µs, MAX_AUTO_SHUTTER_US)`) is called
after each return to still mode.

`capture_still()` returns `arr[..., ::-1].copy()` — a BGR→RGB channel swap, with
the comment "libcamera delivers BGR despite RGB888 label on Pi 5". **Note the
asymmetry:** the standalone `capture.py` (used to build the flat field) does
*not* do this swap. So the flat-field reference and the live service stills have
opposite channel order. Because the flat is normalised per-channel to mean ≈1.0,
dividing swaps the per-channel correction; in practice the effect is subtle but
it is a real inconsistency if you ever care about colour fidelity.

`capture_still()` raises `HTTPException(409)` if a recording is in progress.
Heavy timing is logged at DEBUG throughout.

### 2.5 Retention daemon (`retention.py`)

Standalone (`python -m capture.retention`), driven by `celegans-retention.timer`
(2 min after boot, then every 15 min). It re-derives `DATA_ROOT` and the dir
names from env vars (not from `config.py` — there's a comment noting they must
match). Knobs (all env, defaults): `GRACE_HOURS=1`, `MIN_FREE_GB=5`,
`TARGET_FREE_GB=10`, `MAX_AGE_DAYS=30`.

Logic: collect every data file that has an `.acked` sibling. Classify each as a
"max age" violation (file mtime older than `MAX_AGE_DAYS`) or "space pressure"
(acked longer ago than `GRACE_HOURS`). If disk free ≥ `MIN_FREE_GB` **and** there
are no age violations, exit early. Otherwise trash files oldest-acked-first:
age-violation files are always trashed regardless of disk; space-pressure files
stop once free space reaches `max(TARGET_FREE_GB, MIN_FREE_GB)` (the `max` guards
the MIN>TARGET test configuration). Trashing moves the file plus its `.sha256`,
`.acked`, and thumbnail into `.trash/`. `--dry-run` and `--verbose` supported. A
`.retention-last-run` marker is touched on every non-dry run.

---

## 3. Windows launcher (`launcher/`)

A Tkinter desktop app. `main.py` wires up three background threads — `SyncAgent`,
`MotilityAgent`, `CrawlingAgent` — each paired with a thread-safe status object,
and hands all six to `MainWindow`.

### 3.1 Thread model

Every agent follows the same contract, stated in module docstrings and enforced
by convention: the **worker thread only** writes via `status.update()` /
`mark_completed()`; the **UI thread only** reads via `status.snapshot()` /
`pop_completed()`. No widget is ever touched off the main thread. The UI polls
snapshots on `root.after()` timers (`_POLL_MS = 2000` for the main window,
`_PROGRESS_POLL_MS = 200` for the progress dialog). Each status object holds a
`threading.Lock` held only for the brief field copy.

### 3.2 Config (`config.py`)

A `Settings` dataclass persisted to `%APPDATA%\WormScan\config.json`; logs go to
`launcher.log` (rotating, 1 MB ×3). Fields and defaults:

- `pi_url = "http://192.168.50.2:8000"`, `token = ""`, `mirror_root = ~/Documents/WormScan`, `poll_interval_s = 120`.
- Analysis: `tierpsy_image = "tierpsy/tierpsy-tracker"`, `tierpsy_image_tag = "latest"`, `docker_command = "docker"`, `analysis_video_timeout_s = 600`, `motility_long_threshold_s = 5.0`.

**Drift to know about:** `ui.py` reads `getattr(self._settings, "crawling_min_track_s", 60)`
and writes a "Min track duration" spinbox, but **there is no `crawling_min_track_s`
field on `Settings`** — so it never persists; the spinbox always defaults to 60
on launch. (The value *is* used for the run via `start_analysis(min_track_s=...)`,
it just isn't remembered.) Similarly, `motility_long_threshold_s` *is* persisted
on each motility start.

`load()` filters unknown keys against the dataclass fields, so a stale config
file won't crash the app.

### 3.3 Sync flow (`sync.py`)

`SyncAgent` is a daemon thread. On start it cleans up leftover `.partial` files,
does a one-shot clock sync, then loops: every `poll_interval_s` (or when woken by
the "Sync now" button) it `GET /manifest`, and for every non-acked entry in
`pictures`, `videos`, and each session, it:

1. Computes the local mirror path. If the file already exists locally with a matching SHA256, skip the download.
2. Otherwise downloads to `<dest>.<sha256[:8]>.partial`, streaming and hashing as it goes; verifies the full hash; `os.replace` into place (atomic on Windows). Mismatch → discard, retry next tick.
3. `POST .../ack` with `{relative_path, sha256}`.

The `.partial` naming lets `_cleanup_partials` reason about leftovers on
restart without the manifest: it deletes orphans and already-completed partials
alike, never touching a good target file.

**Naming convention for the local mirror.** `_build_name_maps` turns Pi metadata
into friendly, collision-safe folder names. Sessions mirror to
`mirror/experiments/<experiment_name>/<condition_dir>/<plate NN>/<filename>`,
where `condition_dir` comes from the manifest's `condition_name`. Collisions in
sanitised names get a ` (<id[:6]>)` suffix. Filenames are sanitised
(`\/:*?"<>|` → `_`, trimmed, ≤200 chars). Pictures/videos mirror to
`mirror/pictures/<date>/...` and `mirror/videos/<date>/...`. Session entries
missing the friendly metadata fall back to `experiments/<session_id>/<rel_path>`.

`SyncStatus` carries a colour (`green`/`yellow`/`red`/`gray`), label, last-sync
time, cumulative files/bytes mirrored, and an ephemeral clock-sync message with a
5 s display window.

### 3.4 UI structure (`ui.py`)

`MainWindow` shows a status dot + label, four action buttons (Open Imaging UI,
Open Analysis, Open Mirror Folder, Sync now), a "Shut down Pi" button, and a
settings button. "Open Imaging UI" opens `{pi_url}/?token={token}` in the
browser. The status dot prioritises a running analysis over sync state; clock-sync
messages briefly override the sync label. The "Sync now" button locks out until
the next green sync (`_button_waiting`).

`SettingsDialog` edits pi_url/token/mirror/poll-interval (poll must be ≥10).

`AnalysisDialog` is where analysis is launched. Radio buttons select Motility,
Crawling, or Counting (Counting is permanently `disabled` with a "Not yet built"
tooltip). It has a folder picker (defaults to `mirror_root`), a motility-only
"Min fragment length (s)" spinbox (1.0–30.0, hidden when Crawling is selected
because it's inert there), a "Clear cache before run" checkbox, and two mutually
exclusive render-option frames:

- Motility renders: Tracked, Curvature, Side-by-side, Per-worm curvature traces.
- Crawling renders: Tracked, Side-by-side, Path traces, plus a "Min track duration (s)" spinbox (1–600).

On Start it validates inputs, runs `run_preflight` (Docker installed/running,
Tierpsy image present, ffmpeg+ffprobe present, ≥1 mp4 found), persists the
motility threshold, opens a modeless `AnalysisProgressDialog`, and calls the
chosen agent's `start_analysis`. The progress dialog polls the status snapshot,
drives a determinate bar by `current_index/total`, shows the current stage, and
rotates a list of joke "flavour" strings. Cancelling sets the agent's cancel
event.

**Two UI bugs worth noting:** `_on_settings_saved` updates `self._agent` (sync)
and `self._motility_agent` but **not** `self._crawling_agent` — the crawling
agent keeps stale settings after a settings change until app restart. And the
`threshold_s` validation in `_start` always runs even in crawling mode where the
value is inert.

### 3.5 setup.bat / venv

`setup.bat` lives in `launcher/` and resolves repo root as its parent. It is
additive/idempotent and needs no admin rights: checks Python ≥3.11 on PATH,
creates `launcher/.venv` if absent, `pip install -r launcher/requirements.txt`,
and writes a Desktop `WormScan.lnk` (via a temp PowerShell script) targeting
`launcher/.venv/Scripts/pythonw.exe "launcher/main.py"` with WorkingDirectory =
repo root and the `wormscan.ico` icon. Dev launch path is the documented
`source launcher/.venv/Scripts/activate && python launcher/main.py`.

`launcher/requirements.txt`: requests, pandas, matplotlib, tables, h5py, numpy,
openpyxl, opencv-python, imageio-ffmpeg, scipy. (Note `CLAUDE.md` claims the
launcher needs "requests (only)" — **stale**; the analysis pipeline pulled in the
whole scientific stack.)

---

## 4. Motility pipeline — MP4 to Excel, step by step

Entry point: `MotilityAgent._run_analysis` (`launcher/analysis/motility.py`).
Runs on the motility worker thread. Outputs land in
`<selected_folder>/_analysis_<YYYY-MM-DD_HHMMSS>/`.

### 4.1 Discovery and per-video loop

`find_videos` (`ffmpeg_utils.py`) walks the chosen folder up to 3 levels deep,
collecting `*.mp4`, skipping any directory whose name starts with `_` (so prior
`_analysis_*` and `_wormscan_cache` dirs are ignored). Folder depth maps to
labels via `_resolve_video_path`: a video directly in the root → `condition="default", plate=<stem>`;
one level down → `condition="default", plate=<parent>`; two+ levels →
`condition=<grandparent>, plate=<parent>`.

If "Clear cache" is set, every `_wormscan_cache` dir under the folder is
`rmtree`'d first. Each video gets a cache dir at
`<video_parent>/_wormscan_cache/<stem>/`.

### 4.2 Probe + transcode (ffmpeg)

- `probe_fps` runs `ffprobe ... stream=r_frame_rate`, parsed through `fractions.Fraction` → float.
- `probe_duration` runs `ffprobe ... format=duration`.
- `convert_to_avi` transcodes the MP4 to an **MJPEG AVI at quality 3**:

  ```
  ffmpeg -y -i <mp4> -vcodec mjpeg -q:v 3 <cache>/<stem>.avi
  ```

  (skipped if the AVI already exists). MJPEG is used because Tierpsy reads it
  reliably and it's seekable frame-by-frame for the renders. All ffmpeg/ffprobe
  calls pass `creationflags=CREATE_NO_WINDOW` on Windows.

### 4.3 Caching

A cache "hit" means `<cache>/Results/<stem>_featuresN.hdf5` exists and contains
`/trajectories_data`. On a hit, Tierpsy is skipped; the AVI is only (re)generated
if a render was requested and the AVI is missing. On a miss, the pipeline
transcodes, writes the per-video params JSON, and runs Tierpsy.

### 4.4 The Tierpsy Docker invocation

`run_tierpsy` (`docker_utils.py`). Tierpsy's `tierpsy_process` is batch-oriented
(it scans a directory for a filename pattern), so the call mounts the video's
**parent cache dir** as `/data` and passes the AVI basename as the include
pattern, isolating the single file:

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
by default. Timeout = `settings.analysis_video_timeout_s` (600 s). On timeout or
non-zero exit → `RuntimeError` (last 1000 chars of stderr).

Tierpsy writes `MaskedVideos/<stem>.hdf5` and `Results/<stem>_featuresN.hdf5`,
`<stem>_skeletons.hdf5`, etc., inside the mounted cache dir.

### 4.5 Tierpsy parameters we override (`motility_params.json`)

The whole JSON is written per-video (with `expected_fps` patched to the probed
fps and the WormScan-only key `head_angle_prominence` stripped before Tierpsy
sees it). Full set with current values:

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
save_full_interval       -1
compression_buff         -1
keep_border_data         false
is_light_background      false        ← dark worms on light background = false here
is_extract_timestamp     true
expected_fps             30.0         ← overwritten per-video with probed fps
microns_per_pixel        -1.0         ← uncalibrated; all distance metrics are in pixels
mask_bgnd_buff_size      -1
mask_bgnd_frame_gap      -1
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
resampling_N             49           ← skeletons are 49 points; the post-processing assumes this
max_gap_allowed_block    -1
ht_orient_segment        -1
filt_bad_seg_thresh      0.1
filt_max_width_ratio     2.25
filt_max_area_ratio      6
filt_min_displacement    0
filt_critical_alpha      0.01
int_save_maps            false
int_avg_width_frac       0.3
int_width_resampling     15
int_length_resampling    131
int_max_gap_allowed_block -1
head_tail_int_method     MEDIAN_INT
split_traj_time          90
ventral_side             ""
feat_skel_smooth_window  5
feat_coords_smooth_window_s 0.25
feat_gap_to_interp_s     0.25
feat_derivate_delta_time 0.33
n_cores_used             1
nn_filter_to_use         none         ← classic CV pipeline, no neural net
path_to_custom_pytorch_model ""
use_nn_food_cnt          false
MWP_*                    multi-well-plate keys, all effectively off (n_wells -1)
head_angle_prominence    0.50         ← WormScan-only; NOT sent to Tierpsy (see below)
```

`head_angle_prominence` is the one key our code consumes and Tierpsy never sees:
`_WORMSCAN_ONLY_KEYS` is popped from the dict before the JSON is written.

**`microns_per_pixel = -1.0` means there is no spatial calibration.** Every
distance/speed metric in the outputs is in **pixels** (and px/s), not microns,
despite some column names that don't say "px".

### 4.6 crawling_params.json differs — the "same params" docstring is stale

`crawling.py`'s module docstring says crawling is "a near-exact copy ... same
Tierpsy parameters". It is **not** the same params anymore — `crawling_params.json`
diverges from `motility_params.json` on the masking/tracking knobs:

| key | motility | crawling |
|-----|----------|----------|
| `mask_min_area` | 50 | 500 |
| `thresh_C` | 10 | 5 |
| `thresh_block_size` | 61 | 31 |
| `worm_bw_thresh_factor` | 1.05 | 1.0 |
| `traj_min_area` | 25 | 500 |
| `traj_max_allowed_dist` | 100 | 30 |
| `filt_min_displacement` | 0 | 100 |

Crawling is tuned for fewer, larger, well-separated moving worms; motility for
many small/curling ones. The two `head_angle_prominence` values are both 0.50.

### 4.7 The bend-rate algorithm (as actually implemented)

This is the most important and most mis-documented part. **Bends are counted from
a head-swing angle, not from `curvature_midbody`.** Anything you've read that says
"bend rate from curvature_midbody" is describing the *curvature render video*
(§4.10), not the BPM number.

The core lives in `analysis_csv.py`:

**Signal construction (`compute_head_angle_signal`):** for each frame of a
worm track, look up its skeleton (49×2 array, indexed by `skeleton_id` into
`/coordinates/skeletons`). Compute two vectors from skeleton points:

- `head_vec = skel[0] - skel[5]` (head tip relative to a near-head point),
- `body_vec = skel[20] - skel[30]` (a mid-body direction),

and take the **signed angle between them** via
`atan2(cross, dot)` (radians). Frames with a missing/non-finite skeleton are NaN.
Tracks with fewer than 10 valid-angle frames are dropped (`return None`).

**Detrend (`_detrend`):** smooth the angle series with a centred rolling mean
over a `0.3 s` window (`max(3, int(fps*0.3)|1)`), then subtract a slow baseline =
a centred rolling mean over a `2.0 s` window. The result is the detrended
head-angle signal. (This 2 s baseline is the "2-second rolling mean" referenced
elsewhere — it's applied to head angle, not curvature.)

**Peak detection:** `scipy.signal.find_peaks` on the detrended signal for
positive peaks and on its negation for negative peaks, both with
`prominence = head_angle_prominence`. **The effective prominence is 0.50 rad**,
read from `motility_params.json`. (Watch out: the *function-signature defaults*
for `head_angle_prominence` are `0.30` in `compute_head_angle_signal`,
`read_fragments`, `make_video_summary_png`, etc. Those defaults are dead — the
real value flows from the JSON via `motility.py:341`. Don't believe the `0.30`.)

**Bends → BPM:** each detected peak is a *half-bend*. `bends = (n_pos + n_neg)/2`.
BPM = `bends / duration_min`. The duration denominator differs by group type:

- **Curl groups** (`_metrics_curl`): the bend counter runs on each clean
  sub-track independently and peaks are summed; `duration_min = total_clean_s/60`,
  where `total_clean_s` is the summed length of all clean sub-tracks. (The most
  recent commit, `bbfcd1e`, changed the curl denominator to `total_clean_s` and
  removed a "curl bonus".)
- **Collision sub-tracks** (`_metrics_one_collision_subtrack`): uses
  `bends_per_minute(sig)`, whose denominator is `signal["n_valid"]/fps/60` — the
  count of **valid-angle frames** in that one sub-track, not the sub-track's wall
  length. So curl and collision BPM use subtly different denominators. Worth
  knowing if you're comparing the two classifications.

`bend_interval_cv` = coefficient of variation of inter-peak intervals (seconds);
NaN with <3 peaks. Lower = more regular rhythm.

The block is headed by a comment: *"Bend counter — UNCHANGED from v1 ... Do not
modify."* Yet every row is stamped `bend_method = "head_angle_peaks_v2"`. So the
"v1/unchanged" comment and the "v2" label disagree; the label is what's written
to the output.

### 4.8 Fragment grouping, flicker filter, debris filter

Tierpsy fragments a single worm's track when it self-touches (curls) or worms
collide. The pipeline reassembles them before scoring.

**Grouping (`fragment_grouping.py`).** One `FragmentInfo` per
`worm_index_joined` (start/end frame, mean centroid of first/last 5 frames,
wall-clock times). Two fragments are adjacent if the later one starts within
`TIME_GAP_THRESHOLD_SECONDS = 5.0` of the earlier ending *and* their
end→start centroid distance ≤ `DISTANCE_THRESHOLD_PIXELS = 50`. Union-find over
those edges yields connected components. A component is classified **curl** if
every node has ≤1 in- and ≤1 out-edge within the component (a linear chain),
otherwise **collision** (branching). Solo fragments are size-1 curl groups. The
representative track is the earliest-starting fragment.

**Flicker filter (`flicker_filter.py`).** Per track, compute per-frame skeleton
length (sum of segment lengths of the 49-point skeleton). Take a centred rolling
std over a `FLICKER_WINDOW_SECONDS = 0.5` window; flag a frame as flicker if the
skeleton is missing **or** the rolling std exceeds
`FLICKER_STD_THRESHOLD_PIXELS = 20`. Split the track into contiguous clean
sub-tracks at the flagged boundaries. (The threshold comment admits it's a
starting value expected to tighten.)

**Per-group processing (`read_fragments`).**
- Curl group: if total clean observation time < `MIN_OBSERVATION_TIME_SECONDS = 10.0`, drop the whole group (reason `curl_too_short` or, if zero clean frames, `flicker_killed_track`). Otherwise compute curl metrics.
- Collision group: estimate the max number of concurrently-active clean sub-tracks (`_max_concurrent`), cap at `COLLISION_WORM_COUNT_CAP = 3`, select that many overlapping sub-tracks maximising frame count, and emit one worm row per selected sub-track that individually clears the 10 s minimum.

**Debris filter** (applied after expansion): a worm row is dropped as debris if
either rule fires:
- Rule 1 (stationary): `displacement_px < 8.0` and `bpm < 5.0`.
- Rule 2 (flickery blob): `length_cv > 0.10` **and** `solidity_median > 0.6` **and** `speed_median_abs < 10.0` (all three must be finite and pass).

Shape metrics (`length_cv`, `solidity_median`, `speed_median_abs`) come from
Tierpsy's `timeseries_data` (length, speed) and `blob_features` (solidity), best
effort — NaN if unavailable.

Everything that happens to each track is logged to a per-video sidecar
`per_video/<condition>__<plate>_analysis_log.json` (input track count, groups
formed by type, drops by reason, flicker stats, fragment-count distribution,
collision expansion counts, and a `dropped_tracks` list with per-track reasons).

### 4.9 Per-worm row schema and the `is_long` gate

Each surviving worm becomes a row with (among others): `condition`, `plate`,
`worm_index` (sequential, assigned post-filter), `repr_tierpsy_id`,
`member_tierpsy_ids`, `frames`, `duration_s`, `bpm`, `bend_interval_cv`,
`is_long`, `coverage_pct`, `is_full_track`, `group_classification`, `curl_count`,
`fragment_count`, `valid_frac`, `displacement_px`, `length_cv`,
`solidity_median`, `speed_median_abs`, `group_id`.

- For curls, `duration_s` is the wall-clock span (earliest fragment start to latest fragment end); `valid_frac` = clean time / span.
- For collisions, `duration_s` = sub-track length / fps; `valid_frac` = 1.0.
- `is_long = duration_s >= long_threshold_s` (the UI "Min fragment length", default 5 s). **Only long worms feed the summary statistics.**
- `coverage_pct` = clean frames / total video frames × 100. `is_full_track = coverage_pct >= 90`. **`is_full_track` is computed but never read anywhere** — dead column, and it isn't even exported (see below).

### 4.10 Outputs

Written to the `_analysis_<timestamp>/` directory:

- **`motility_results.xlsx`** — one sheet per condition (sheet name sanitised+deduped, ≤31 chars), rows sorted by `duration_s` descending, with this fixed column set: `plate, worm_index, repr_tierpsy_id, group_id, frames, duration_s, bpm, bend_interval_cv, is_long, fps_used, group_classification, curl_count, fragment_count, valid_frac, displacement_px, coverage_pct, length_cv, solidity_median, speed_median_abs`. Plus a `_summary` sheet (one row per video). Note `is_full_track`, `member_tierpsy_ids`, `bend_method` are in the row dicts but **not** in the exported column list.
- **`motility_summary.csv`** — the `_summary` rows again as CSV. Per video: counts of total/long fragments, `bpm_{median,mean,std,min,max}_long`, `bend_cv_{mean,median}_long`, median `length_cv`/`solidity`/`speed` over long worms, curl/collision counts among long worms, mean valid-frac and fragment count, `fps_used`, `duration_video_s`, and `status` (`"ok"` or the truncated exception string).
- **`overview.png`** — `make_overview_png`: a box-and-whisker of median-BPM-per-video grouped by condition (with jittered points and per-condition n), or a per-plate bar chart when there's only one condition. Title includes total videos and elapsed minutes.
- **`per_video/<condition>__<plate>.png`** — `make_video_summary_png`: two panels — a sorted BPM bar chart of long fragments with the median line, and detrended head-angle traces for the 3 longest fragments with detected peaks marked. Y-axis labelled "Head angle (rad)".
- **`per_video/<...>_analysis_log.json`** — the per-track decision log (§4.8).
- **`log.txt`** — human-readable run log.

**Optional renders** (`render_video.py`), each adding ~30–90 s/video and
requiring the AVI present:
- `_tracked.mp4` — skeleton polylines + worm-index labels. Fragments belonging to a kept worm are coloured by stable `worm_index` (12-colour palette, persists across that worm's fragments) and labelled with the worm_index; fragments that were filtered out get a faint grey centroid dot only (the worm_index_map / "Phase 3" behaviour).
- `_curvature.mp4` — skeleton coloured by the **sign of detrended `curvature_midbody`** (red positive, blue negative, grey zero). This is the *only* place `curvature_midbody` is used, and it uses the same 0.3 s/2.0 s smooth+detrend windows as the head-angle path — but on a different signal. Do not confuse it with the BPM computation.
- `_sidebyside.mp4` — original frame beside masked+tracked frame.
- `<...>_traces/worm_<id>.png` + `.mp4` — per fully-long worm: a static head-angle trace PNG and a two-panel MP4 (cumulative trace left, cropped masked video with skeleton overlay right). Note these iterate over `is_long` worms (the variable is named `full_track_rows` but filters on `is_long`, not `is_full_track`).

### 4.11 Worm-index → render mapping

Before rendering, `motility.py` builds `worm_index_map: {tierpsy_id -> worm_index}`
from each kept row's `member_tierpsy_ids` (or `repr_tierpsy_id`). The renders key
on `worm_index_joined` (the `_skeletons.hdf5` trajectory id), so this map is what
lets a render colour/label by the grouped, stable worm number that appears in the
Excel. The renders read `_skeletons.hdf5` (49-point `/skeleton` dataset +
`trajectories_data`) and `MaskedVideos/<stem>.hdf5` (`/mask`), lazily one frame at
a time, piping raw BGR24 into `ffmpeg -vcodec libx264 -preset fast -crf 22
-pix_fmt yuv420p`.

---

## 4b. Crawling pipeline (`analysis/crawling.py`, `crawling_metrics.py`)

A second, parallel pipeline launched from the same dialog. Structurally it mirrors
motility (same thread contract, same cache layout, same ffmpeg/AVI step, same
Tierpsy invocation shape) but:

- Uses **`crawling_params.json`** (different masking/tracking knobs, §4.6).
- Tierpsy is run through `_run_tierpsy_instrumented`, which streams docker
  stdout+stderr line-by-line to the console, prints the exact command, and dumps
  a recursive listing of the output dir on exit. Its timeout is **hardcoded to
  `_TIERPSY_TIMEOUT_S = 3600`** (not the 600 s settings value).
- Metrics are computed **per `worm_index`** directly from Tierpsy's
  `timeseries_data` (no fragment grouping, no flicker filter). `compute_crawling_metrics`
  emits one row per worm with: mean/forward/backward speed (px/s), fraction
  forward/backward/paused, reversal count + rate/min, centroid `path_length_px`,
  `net_displacement_px`, `tortuosity`, mean length/width (px), `track_duration_s`,
  `longest_continuous_run_s`, `skeleton_coverage`, and the **legacy head-angle
  `bend_rate_bpm`** kept as one extra column (computed via the shared
  `compute_head_angle_signal`/`bends_per_minute`).
- "Paused" = |speed| below 10% of the video-wide median |speed|. Reversals are
  forward→backward transitions in `motion_mode` (fallback: sign changes in speed).
- `skeleton_coverage` and `longest_continuous_run_s` are read from the
  **`_skeletons.hdf5`** `trajectories_data` (`has_skeleton` column lives there, not
  in `_featuresN.hdf5`).
- **Quality filter** (`_passes_filter`): keep a worm if
  `longest_continuous_run_s >= min_run_s` (UI "Min track duration", default 60 s)
  **and** `skeleton_coverage >= SKELETON_COVERAGE_MIN = 0.3`. Raw per-worm rows are
  always kept in the output; the filter is recomputed at aggregation time, so you
  can re-tune thresholds on a saved `per_worm` table without re-running Tierpsy.

Outputs: `crawling_results.xlsx` (sheets `per_worm` and `per_condition`),
`crawling_summary.csv` (the per-condition aggregates), `log.txt`, and the optional
renders (Tracked, Side-by-side via the shared `render_video.py`; Path traces via
`crawling_render.py` — a darkened video with fading per-worm centroid trails over
a 10 s window). The renders restrict to filter-passing `kept_ids`. There is **no
overview PNG and no per-video summary PNG** for crawling.

`crawling.py` accepts a `threshold_s` argument (passed from the dialog) but never
uses it — only `min_track_s` matters for crawling.

---

## 5. Configuration surface

**Pi service env vars** (`capture/.env`, prefix `CELEGANS_`, loaded by
pydantic-settings; `.env.example` is the committed template):

- `CELEGANS_TOKEN` (required, the shared bearer token).
- `CELEGANS_DATA_ROOT` (default `/home/pi/celegans-data`).
- `CELEGANS_HOST` (`0.0.0.0`), `CELEGANS_PORT` (`8000`).
- `CELEGANS_MAX_AUTO_SHUTTER_US` (`500000` = 500 ms AE shutter cap, so dim scenes go dark rather than freezing the frame rate).
- Retention (read by `retention.py`, not the service): `CELEGANS_RETENTION_GRACE_HOURS=1`, `..._MIN_FREE_GB=5`, `..._TARGET_FREE_GB=10`, `..._MAX_AGE_DAYS=30`.

The systemd capture unit substitutes `${CELEGANS_HOST}`/`${CELEGANS_PORT}` from
the EnvironmentFile into the uvicorn `ExecStart`.

**Hardcoded paths / magic constants:**

- Pi network: `192.168.50.2` (Pi), `192.168.50.1` (laptop), SSH alias `celegans`. The launcher default `pi_url` bakes in `http://192.168.50.2:8000`.
- Camera: `FULL = 4056×3040`, `VIDEO = 2028×1520 @ 30 fps`, `PREVIEW = 1280×960`, default video bitrate `9_000_000` bps, default capture duration `30 s` (`capture_ops.py`).
- Flat-field master filename: `master_flat.npy`, averaged from `FLAT_N_FRAMES = 16` frames.
- Skeletons are `resampling_N = 49` points; head-angle uses indices 0/5 (head) and 20/30 (body); flicker rolling-std window 0.5 s, threshold 20 px; detrend windows 0.3 s / 2.0 s; bend prominence 0.50 rad.
- Pipeline thresholds (`analysis_csv.py` module constants): distance 50 px, time gap 5 s, min observation 10 s, collision cap 3, debris displacement 8 px / bpm 5 / length_cv 0.10 / solidity 0.6 / speed 10.
- Crawling: `LONGEST_RUN_MIN_S = 60`, `SKELETON_COVERAGE_MIN = 0.3`, paused = 10% of median speed, path-trace window 10 s, darken 0.5.
- Render encode: libx264, preset fast, crf 22, yuv420p; worm-ID font scale 1.4 / thickness 3 (tuned for ~2028×1520 frames).

**Auth mechanism:** one shared token, `secrets.compare_digest`, header or query
param. The web UI prompts for it and stores it client-side; the launcher persists
it in `config.json` and passes it as `X-Auth-Token`. The preview stream and the
"Open Imaging UI" deep-link both use the `?token=` form because they can't set
headers.

---

## 6. Known deviations and rough edges

Collected from reading, roughly in order of how likely they are to bite you.

1. **Camera identity is wrong in `CLAUDE.md`.** Hardware is IMX477 HQ Camera (4056×3040), confirmed by `capture.py` and `camera.py`. `CLAUDE.md` says IMX708 Module 3. (§2.1)

2. **On-disk layout is wrong in `CLAUDE.md`.** The doc's `sessions/` + `freecapture/` tree doesn't exist; the real names are `experiments/`, `pictures/`, `videos/` (`config.py`). The launcher and retention daemon hardcode the real names. (§2.3)

3. **Flat-field directory mismatch — likely a real bug.** `capture_ops.load_flat` reads the master flat from `DATA_ROOT/flatfield` (`/home/pi/celegans-data/flatfield`). But its own error message tells you to run `python3 capture/capture.py --capture-flat`, and that CLI writes the flat to `<repo>/data/flatfield` (`PROJECT_DIR/data/flatfield`). Following the instructions produces a flat the service can't find. Flat-field correction is opt-in per request, so this stays dormant until someone enables it.

4. **BGR/RGB asymmetry between service and flat reference.** The live `capture_still` swaps BGR→RGB ("libcamera delivers BGR despite RGB888"); the standalone `capture.py` that builds the flat does not. Channels are opposite. Mostly cosmetic because the flat is per-channel normalised. (§2.4)

5. **The bend rate is a head-angle metric, not curvature.** BPM comes from peaks in a detrended head-swing angle (skeleton points 0/5 vs 20/30), prominence 0.50 rad. `curvature_midbody` is only used to colour the optional curvature render video. UI "flavour text" and loose talk conflate the two. (§4.7, §4.10)

6. **`head_angle_prominence` function defaults (0.30) are dead.** The real value (0.50) comes from the JSON via `motility.py`. Every signature default of 0.30 is misleading. (§4.7)

7. **"Bend counter UNCHANGED from v1 / Do not modify" vs `bend_method="head_angle_peaks_v2"`.** The comment and the stamped label disagree; output says v2. (§4.7)

8. **Curl vs collision BPM use different denominators.** Curl divides bends by total clean sub-track seconds; collision divides by valid-angle-frame seconds (`bends_per_minute`). Cross-classification BPM comparisons are not strictly apples-to-apples. (§4.7)

9. **`crawling.py` docstring claims "same Tierpsy parameters" as motility — false.** `crawling_params.json` diverges on seven masking/tracking knobs. (§4.6)

10. **`is_full_track` is computed but never used or exported.** Dead field. The per-worm-trace render filters on `is_long` despite a variable named `full_track_rows`. (§4.9, §4.10)

11. **Excel export drops some computed columns.** `member_tierpsy_ids`, `bend_method`, `is_full_track` exist in the row dicts but aren't in the `_sheet_cols` list, so they never reach `motility_results.xlsx`.

14. **Crawling Tierpsy timeout is hardcoded to 3600 s**, ignoring `settings.analysis_video_timeout_s` (which motility honours at 600 s). The crawling `threshold_s` argument is accepted and ignored. (§4b)

15. **Static file mount is CWD-relative** (`StaticFiles(directory="app/static")`). Only correct because the systemd unit sets `WorkingDirectory=.../capture`. Launch from anywhere else and the web UI 404s. (§2)

16. **`microns_per_pixel = -1.0`: nothing is spatially calibrated.** All "distance"/"speed" outputs are pixels and px/s regardless of column naming. (§4.5)

17. **`CLAUDE.md` says the launcher needs "requests (only)".** It actually needs the full scientific stack (pandas, numpy, h5py, opencv, scipy, matplotlib, openpyxl, tables, imageio-ffmpeg). (§3.5)

18. **Mixed async/sync in the session router.** `delete_session`/`delete_condition`/`delete_plate`/`capture`/`ack` run via `asyncio.to_thread`, but `create_session` and `add_plate` run synchronously on the event loop. Fast enough that it doesn't matter, but it's inconsistent.

19. **`clock-sync` and `shutdown` depend on sudoers entries** for `/bin/date` and `/sbin/shutdown`. The clock-sync error message documents the needed line; if it's missing the endpoint 500s.

20. **Untracked debugging cruft in the working tree.** Root-level `check_*.py` and `inspect_filter_decisions*.py` have hardcoded `C:\Users\Isabe\Desktop\Tierpsyclips\...` paths; `launcher/tools/` holds a param-sweep harness, a clip cutter, and skeleton/head-angle inspectors. None are imported by the app. Several tracked files (`analysis_csv.py`, `plots.py`, `main.py`, `motility_params.json`, `tierpsy_param_sweep.py`) currently have uncommitted modifications — this snapshot reflects the working-tree contents, not `HEAD`.
