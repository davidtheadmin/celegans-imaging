# Phase 3 — COMPLETE (all bugs fixed)

All backend and frontend work for Phase 3 is merged and pushed.

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
- **Video deadlock fixed**: all picamera2 calls serialized through `_capture_lock`; `_preview_loop` holds lock during `capture_array("lores")` only (JPEG encode outside lock); `measure_fps()` removed — it called `capture_request()` which deadlocked against `capture_array("lores")` on libcamera's internal frame queue

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

## Resumed in next session

**Phase 5a** is the next planned work.

Phase 5a scope (to be confirmed at session start):
- Analysis service skeleton (`analysis/` directory)
- Motility scoring: per-clip worm-movement metric from recorded MP4s
- Results written back to plate directory (e.g. `results.json`)
- API endpoints to fetch per-plate and per-session results
- Frontend: results column in the condition/plate tree; sparkline or score badge per plate
