# Phase 3 Status — Polish Complete

## What's working

### Backend
- `GET /sessions/{id}/plates/{plate_id}/files/{filename}?thumb=1` — plate file serving; thumbnails cached in `.thumbs/`
- `GET /capture/free/files?date=` — lists free-capture files for a given date
- `GET /capture/free/files/{date}/{filename}?thumb=1` — serves free-capture files with thumbnail support
- `DELETE /sessions/{id}/plates/{plate_id}/files/{filename}` — soft delete (moves to `.trash/`)
- `DELETE /capture/free/files/{date}/{filename}` — soft delete free-capture file
- `DELETE /sessions/{id}/plates/{plate_id}` — moves entire plate folder to `.trash/`, removes from manifest
- `POST /sessions/{id}/plates` now accepts `replicates: int` (1–50); creates N plates in one call
- Video thumbnails: `make_thumb` extracts first frame via `ffmpeg -frames:v 1` for `.mp4/.h264/.mkv`
- Magnifier endpoint (`/magnifier.jpg`): serves lores preview crop (no full-res capture); near-instant
- Color fix: `capture_still()` flips BGR→RGB (libcamera delivers BGR despite RGB888 label on Pi 5)

### Frontend (`capture/app/static/`)
- Dark instrument-style UI — GitHub dark palette, monospace readouts, no framework
- Status bar: connection dot, camera state, AE lock with exposure values, disk free (color-coded), **?** shortcut button
- MJPEG live preview with focus score updated every 500ms
- AE lock/unlock — locked values shown in header chip and preview overlay
- Magnifier on M-hold (button + keyboard); 100ms polling against lores-based endpoint (fluid)
- Free Still capture with filename feedback + thumbnail strip
- Free Video capture with duration input and live countdown progress bar
- Session sidebar: create session, expand/collapse (state persists in sessionStorage), add plates
  - Add-plate form: pre-fills condition/name from last plate, auto-increments number
  - **Replicates field**: create N plates of same condition in one submission (shows "Plates X–Y" preview)
  - **Delete plate**: × button on hover, confirm dialog, moves to `.trash/`
- Session capture panel adapts to assay mode
- Quadrant 2×2 grid: completion state derived from files on disk on every plate visit; captured quadrants green
- Thumbnail strip: last 8 captures; click → full-size modal
  - **Video tiles**: poster frame from ffmpeg + play circle overlay
  - **× delete button** on hover; after delete: dashed tombstone tile stays in strip
  - **Modal Delete button** for the open file
  - **Video modal**: `<video controls>` for `.mp4` files
- Token prompt on load; `?token=` URL param captured and URL-cleaned
- Polling pauses when tab is hidden
- **Keyboard shortcuts** (disabled in form inputs):
  - `Space` — primary capture for current mode/plate
  - `1–4` — NW/NE/SW/SE quadrant capture
  - `N` — open add-plate form
  - `L` — toggle AE lock
  - `M` hold — magnifier
  - `Esc` — close modal/magnifier/shortcuts (priority order)
  - `?` — shortcut reference overlay
- **Inline kbd hints** `[Key]` on all capture buttons, quadrant labels, magnifier, AE lock

## Performance (Phase 2 baseline)

| Operation | Wall clock |
|-----------|-----------|
| `POST /capture/free/still` | ~0.42s |
| `POST /sessions/.../capture` (survival, single) | ~0.38s |
| `POST /sessions/.../capture` (survival, quadrant) | ~0.38s |
| `GET /magnifier.jpg` (lores crop, no capture) | <10ms |

## Next: Phase 4
