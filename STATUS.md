# Phase 3 Status — Polish Complete (Round 2)

## What's working

### Backend
- `GET /sessions/{id}/plates/{plate_id}/files/{filename}?thumb=1` — plate file serving; thumbnails cached in `.thumbs/`
- `GET /capture/free/files?date=` — lists free-capture files for a given date
- `GET /capture/free/files/{date}/{filename}?thumb=1` — serves free-capture files with thumbnail support
- `DELETE /sessions/{id}/plates/{plate_id}/files/{filename}` — soft delete (moves to `.trash/`)
- `DELETE /capture/free/files/{date}/{filename}` — soft delete free-capture file
- `DELETE /sessions/{id}/plates/{plate_id}` — moves entire plate folder to `.trash/`, removes from manifest
- `POST /sessions/{id}/plates` accepts `replicates: int` (1–50); creates N plates; 409 on duplicate `(condition_id, name, plate_number)`
- Plate id format: `{condition_id}_{name}_{NN:02d}` (changed from `{condition_id}_{NN}` for cross-condition uniqueness; old sessions remain accessible)
- Video thumbnails: `make_thumb` extracts first frame via `ffmpeg -frames:v 1` for `.mp4/.h264/.mkv`
- Color fix: `capture_still()` flips BGR→RGB
- **Video framerate fix**: `wrap_h264()` now uses `-r <fps>` (not `-framerate`, which is a V4L2-only flag silently ignored for file inputs); actual fps measured from `FrameDuration` camera metadata before recording; falls back to `DEFAULT_VIDEO_FPS = 30`

### Frontend (`capture/app/static/`)
- Dark instrument-style UI — GitHub dark palette, monospace readouts, no framework
- Status bar: connection dot, camera state, AE lock with exposure values, disk free (color-coded), **?** shortcut button
- MJPEG live preview with focus score updated every 500ms
- AE lock/unlock — locked values shown in header chip and preview overlay
- Free Still capture with filename feedback + thumbnail strip
- Free Video capture with duration input and live countdown progress bar
- Session sidebar: **3-level tree** — session → condition → plates
  - Conditions emerge from grouping the flat plate list by `(condition_id, name)` client-side; no schema change
  - Condition groups collapsible; expand state persists in `sessionStorage` separately from session expand state
  - **Add condition form**: Name first, then Condition ID, then replicates count; shows "Name / CondID — plates 1 through N" preview; validates no duplicate `(condition_id, name)` before POST; inline error if duplicate
  - **Add N plates** inline per condition: appends from `lastPlateNumber + 1`
  - **Delete plate**: × button on hover, confirm dialog, moves to `.trash/`
- Session capture panel adapts to assay mode
- Quadrant 2×2 grid: completion state derived from files on disk; captured quadrants green
- Thumbnail strip: last 8 captures; click → full-size modal
  - Video tiles: poster frame + play circle overlay
  - × delete button on hover; tombstone tile after delete
  - Modal Delete button; `<video controls>` for `.mp4` files
- Token prompt on load; `?token=` URL param captured and URL-cleaned
- Polling pauses when tab is hidden
- **Keyboard shortcuts** (disabled in form inputs):
  - `Space` — primary capture for current mode/plate
  - `1–4` — NW/NE/SW/SE quadrant capture
  - `N` — open Add Condition form for active session
  - `L` — toggle AE lock
  - `Esc` — close modal / shortcuts overlay
  - `?` — shortcut reference overlay

### Removed / changed in round 2
- **Magnifier removed**: the 100ms polling loop to `/magnifier.jpg` caused service hangs in real lab use. Endpoint, button, JS polling loop, CSS, and M hotkey all removed. Future rethink: CSS zoom on the existing MJPEG stream.
- **Video playback speed fixed**: was 3–5× too fast because the H264 demuxer ignored `-framerate`; fixed with `-r` and measured actual camera fps.
- **Add-plate form replaced by Add-condition form**: plate numbers now reset to 1 per condition; N hotkey updated.

## Performance (Phase 2 baseline)

| Operation | Wall clock |
|-----------|-----------|
| `POST /capture/free/still` | ~0.42s |
| `POST /sessions/.../capture` (survival, single) | ~0.38s |
| `POST /sessions/.../capture` (survival, quadrant) | ~0.38s |

## Next: Phase 4
