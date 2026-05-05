## 2026-05-05

**Bend-counting algorithm replaced with head-angle peak counting.** Validated against lab technician's manual counts on 8 worms (4 fast WT, 4 slow). MAE 1.8 bends/30s vs prior method's 5.1. Old midbody-curvature method overcounted noise on slow/dying worms; new method directly implements the lab's manual protocol. `bend_calibration.py` kept in repo as reference implementation and regression validator.



## 2026-05-02

**Motility analysis pipeline complete.** Validated end-to-end on reference video (test.avi: 30s, ~15 worms, median 57.1 BPM matching prior manual run at 56.3 BPM). Pipeline lives in `launcher/analysis/`. User flow: launcher → Open Analysis → Motility → folder picker → progress dialog → results in `_analysis/<timestamp>/`. Tierpsy runs headless in ephemeral Docker containers. Render of tracked MP4s is optional via three checkboxes.

**Outstanding work for v2:**
- Calibrate `microns_per_pixel` (image a stage micrometer, plug value into `motility_params.json`) → unlocks real-units length/speed columns
- Counting analysis branch (YOLOv8 staging on still images, separate pipeline)
- `setup.bat` should auto-sync requirements.txt on launch so future deps don't need manual pip install#


 Phase 6.2 — feat/launcher-v2 (ready to deploy)

## Migration procedure

1. **Stop the service on the Pi**
   ```bash
   ssh celegans "sudo systemctl stop celegans-capture"
   ```

2. **Wipe Pi data** (clean-slate deploy — skip if you want to keep existing data)
   ```bash
   ssh celegans "cd celegans-imaging && bash scripts/wipe_data.sh"
   ```

3. **Run video migration** (if any .mp4 files exist in pictures/ from before this release)
   ```bash
   ssh celegans "cd celegans-imaging && bash scripts/move_videos_out_of_pictures.sh"
   ```
   Idempotent — safe to run on empty directories.

4. **Pull new code and restart service**
   ```bash
   ssh celegans "cd celegans-imaging && git pull && sudo systemctl start celegans-capture"
   ```

5. **Wipe the laptop mirror** (optional, only if starting fresh)
   Delete `Documents\WormScan\experiments`, `\pictures`, `\videos` if present.

6. **Restart the launcher** — clock sync runs automatically on startup.

## Test plan

| Test | Expected result |
|------|----------------|
| a. Free still | File appears in `pictures/<date>/` on Pi; mirrored to `mirror/pictures/` |
| b. Free video | File appears in `videos/<date>/` on Pi; mirrored to `mirror/videos/` |
| c. New experiment with named conditions | After sync: `mirror/experiments/<experiment name>/<condition name>/plate 01/<filename>` |
| d. Pi clock reset | Status line shows "Pi clock synced (offset: Xs)" for ~5 s after launcher starts |
| e. "Sync now" button | Button disables, sync runs immediately, button re-enables |
| f. Rename experiment | Old mirror folder stays orphaned; new files go to new folder (documented BACKLOG limitation) |

## ⚠ Breaking change: manifest key rename

The top-level `/manifest` response renames `freecapture` → `pictures` and
adds a `videos` section. An old launcher reading `manifest["freecapture"]`
will silently get 0 free pictures. Deploy the Pi service and the new
launcher together.

## sudoers note for /clock-sync

The `POST /clock-sync` endpoint runs `sudo -n date -s`. If the FastAPI
service user (`pi`) does not have passwordless sudo for `/bin/date`, the
endpoint returns a 500 with the exact sudoers entry to add:
```
pi ALL=(ALL) NOPASSWD: /bin/date
```
Add to `/etc/sudoers.d/celegans-date` on the Pi.

---

# Phase 5a — COMPLETE (pending Pi deployment + verification)

All six steps committed and pushed. Deploy to Pi, then verify per the checklist below.

---

# Phase 3 — COMPLETE

All backend and frontend work for Phase 3 is merged, pushed, and verified on hardware.
5 consecutive free videos + 3 consecutive motility plate videos — service stayed up,
every video got a valid thumbnail, preview kept running throughout.

---

## What's working

### Backend
- `GET /sessions/{id}/plates/{plate_id}/files/{filename}?thumb=1` — plate file serving; thumbnails cached in `.thumbs/`
- `GET /capture/free/files?date=` — lists free-capture files for a given date
- `GET /capture/free/files/{date}/{filename}?thumb=1` — serves free-capture files with thumbnail support
- `DELETE /sessions/{id}/plates/{plate_id}/files/{filename}` — soft delete (moves to `.trash/`)
- `DELETE /capture/free/files/{date}/{filename}` — soft delete free-capture file
- `DELETE /sessions/{id}/plates/{plate_id}` — moves entire plate folder to `.trash/`, removes from manifest
- `POST /sessions/{id}/plates` accepts `replicates: int` (1–50); 409 on duplicate `(condition_id, name, plate_number)`
- Plate id format: `{condition_id}_{name}_{NN:02d}` — unique across conditions; old `{condition_id}_{NN}` sessions remain accessible without migration
- Video thumbnails: `make_thumb` extracts first frame via `ffmpeg -frames:v 1`; `.thumbs/` dir created before ffmpeg subprocess (was after, causing consistent failures on first use)
- Color fix: `capture_still()` flips BGR→RGB (libcamera delivers BGR on Pi 5)
- **Video framerate**: `wrap_h264()` uses `-r <fps>`; fps measured once at camera startup (before preview thread starts) from `FrameDuration` metadata, stored as `camera_manager.video_fps`; no per-recording measurement
- **Video recording reliability**: 5+ consecutive recordings work without wedging the service; preview keeps running between recordings

### Frontend (`capture/app/static/`)
- Dark instrument-style UI — GitHub dark palette, monospace readouts, no framework
- Status bar: connection dot, camera state, AE lock with exposure values, disk free (color-coded), **?** shortcut button
- MJPEG live preview with Laplacian focus score (500ms poll)
- AE lock/unlock — locked exposure + gain shown in header chip and preview overlay
- Free Still / Free Video capture with progress bar and thumbnail feedback
- Session sidebar — **3-level tree**: session → condition → plates
  - Conditions grouped client-side from flat plate list by `(condition_id, name)`; no schema change
  - Condition collapse state persists in `sessionStorage` independently of session expand state
  - Add Condition form: Name → Condition ID → replicates; preview line; client-side duplicate check; inline error on conflict
  - Add N plates inline per condition: appends from `lastPlateNumber + 1`
  - Delete plate: × button, confirm dialog, moves to `.trash/`
- Session capture panel adapts to assay mode (motility / survival single / survival quadrant)
- Quadrant 2×2 grid: completion state derived from files on disk on every plate visit
- Thumbnail strip: last 8 captures; click → full-size modal; × delete with tombstone; video modal with `<video controls>`
- Token prompt on load; `?token=` URL param consumed and URL-cleaned
- **Keyboard shortcuts**: `Space` capture, `1–4` quadrants, `N` add condition, `L` AE lock, `Esc` close, `?` overlay

### Round 2 polish changes
- **Magnifier removed**: 100ms polling loop to `/magnifier.jpg` caused service hangs in real lab use. Endpoint, button, M hotkey, CSS all removed.
- **Video playback speed fixed**: was 3–5× too fast due to `-framerate` being a V4L2-only flag ignored for file inputs; fixed with `-r` + measured fps.
- **Plate form restructured**: conditions are first-class groups; N hotkey opens Add Condition (not Add Plate).

---

## Lessons learned (picamera2 / libcamera concurrency)

*Recorded here so the next session starts with the right mental model.*

**picamera2 is not thread-safe for concurrent capture calls.**
The libcamera layer underneath serializes frame dispatch through an internal job queue.
Two Python threads calling `capture_array()`, `capture_request()`, or `start_recording()`
simultaneously will race for frames on that queue and deadlock.

**The right locking pattern:**
- All *state-changing* camera operations — `start_recording()`, `stop_recording()`,
  `capture_array("main")`, `capture_request()`, `set_controls()` — must be serialized
  through a single `_capture_lock`.
- *Read-only background consumers of a separate stream* (the lores preview loop calling
  `capture_array("lores")`) must **not** hold that lock. The preview is a passive consumer
  of the lores stream, independent of main-stream state changes. Holding the lock during
  `capture_array("lores")` caused a deadlock: after `stop_recording()` picamera2
  temporarily stalls lores frame delivery, so the preview holds the lock indefinitely,
  and `start_recording()` (which would have un-stalled the camera) can never run.

**ffmpeg framerate must come from static config, not a runtime measurement.**
The original `measure_fps()` called `capture_request()` (grabs ALL streams including
lores) concurrently with the preview's `capture_array("lores")`, deadlocking on the
first try. Fps is now read once at startup — before the preview thread starts — so the
measurement is single-threaded and safe, and the value is reused for all subsequent
recordings via `camera_manager.video_fps`.

---

## Known outstanding / deferred items

- **Magnifier rethink**: a future approach could apply CSS `transform: scale()` on the existing MJPEG `<img>` element (no new endpoint, no polling). Deferred to Phase 5a or later.
- **`plate_info-name` in capture panel**: currently shows `plate.folder_name` (e.g. `10J_WT_plate01`). Could show a friendlier `Name / CondID — plate 01` — left as-is since folder name is unambiguous.
- **Quadrant state on page load**: `markCapturedQuadrants` fires only when a plate is selected, not on app resume. Minor; not causing data loss.
- **No session-level delete**: plates can be deleted but sessions cannot. Will revisit when Phase 4 output files make cleanup more complex.

---

## Performance (Phase 2 baseline, still valid)

| Operation | Wall clock |
|-----------|-----------|
| `POST /capture/free/still` | ~0.42s |
| `POST /sessions/.../capture` (survival, single) | ~0.38s |
| `POST /sessions/.../capture` (survival, quadrant) | ~0.38s |

---

## Phase 5a polish (complete)

### AE shutter cap (`CELEGANS_MAX_AUTO_SHUTTER_US`, default 500 ms)
**Clock sync** ✓ working.
**AE cap** ✓ verified in bright, normal, and dim lighting.

Attempt 1 used `ExposureTimeMax` — not a valid picamera2 control; exposure reports were
stuck (~66 ms) and unresponsive to lighting. Attempt 2 replaces it with
`FrameDurationLimits = (1000, MAX_AUTO_SHUTTER_US)`, which is the correct picamera2
mechanism for bounding both frame time and AE shutter. Trade-off unchanged: image is
darker in very dim conditions but captures never appear hung.

**Observed behaviour**: AE effectively capped at ~66.7 ms (bright: 0.9 ms, normal: 66.7 ms,
very dim: 66.7 ms). The 66.7 ms ceiling corresponds to 1/15 s frame duration — picamera2
pins the frame period there regardless of the 500 ms `FrameDurationLimits` upper bound we
set. Root cause unclear (likely the video config's implicit frame rate or the encoder's
requirement). Effect is the desired one: captures can never hang for more than a few tens
of ms even in abysmal lighting. **Revisit only if we ever need >67 ms exposure.**

### Clock-sync helper
`scripts/sync-pi-clock.sh` — sets Pi clock from laptop system time via SSH.

---

## Useful operations

- **Sync Pi clock** (no internet needed): `./scripts/sync-pi-clock.sh` — run at the start
  of each session when working at a location with no Pi internet access.
- **Data folder migration** (one-time, after deploying feat/polish): stop the service, run
  `bash scripts/rename_data_folders.sh` on the Pi, then restart. Also run the equivalent
  `mv` commands in `Documents\WormScan\` on the laptop mirror
  (`sessions` → `experiments`, `freecapture` → `pictures`).

---

## Phase 5a main work — complete

All six steps committed and pushed. Verification needed on Pi:

1. **SHA256 caching** ✓ committed — capture one of each type, confirm `.sha256` appears.
2. **Manifest endpoints** ✓ committed — curl `/manifest`, `/sessions/{id}/manifest`, `/capture/free/manifest`.
3. **Ack endpoints** ✓ committed — ack a file, verify `.acked` marker; wrong sha256 → 409.
4. **Retention daemon** ✓ committed — `--dry-run` with a backdated `.acked` file; then live run.
5. **Systemd timer** ✓ committed — deploy `.service` + `.timer`, `systemctl list-timers`.
6. **/status extension** ✓ committed — check unsynced fields and `last_retention_run_at` in `/status`.

### Deploy commands for Pi

```bash
ssh celegans "cd celegans-imaging && git pull && sudo systemctl restart celegans-capture"

# Step 5 — retention timer (one-time install)
ssh celegans "sudo cp celegans-imaging/deploy/celegans-retention.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now celegans-retention.timer"
```

### Retention test recipe

```bash
# Capture a file, ack it, backdate the .acked marker
ssh celegans "touch -d '2 hours ago' /home/pi/celegans-data/freecapture/YYYY-MM-DD/FILE.jpg.acked"
ssh celegans "cd celegans-imaging && .venv/bin/python -m capture.retention --dry-run"
# Confirm the backdated file appears as eligible; then run live:
ssh celegans "cd celegans-imaging && .venv/bin/python -m capture.retention"
# Check it moved to .trash/
```

## 2026-05-05

**Bend-counting method calibration revisited.** Tested 8 head-angle variants against manual counts on 8 calibration worms with full-timeline diagnostic plots. Production method (v1: head=5->0, body=30->20, prominence=0.30) remains the best choice across the motility spectrum (MAE 1.8 bends/30s). Anterior-tangent methods (v5/v6) appeared attractive on fast worms but overcount slow worms by 2x - unsafe for survival assays. Worm fast-WT-4 has only 48% valid skeleton frames, which all methods reflect accurately; the 6-bend miss is a tracking issue, not an algorithm issue. Calibration receipts archived in `docs/calibration/`.
