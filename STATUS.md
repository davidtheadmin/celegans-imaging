# Phase 3 Status — Core Complete

## What's working

### Backend
- `GET /sessions/{id}/plates/{plate_id}/files/{filename}?thumb=1` — serves plate files; thumbnail cached in `.thumbs/` subdir
- `GET /capture/free/files?date=` — lists free-capture files for a given date (defaults to today)
- `GET /capture/free/files/{date}/{filename}?thumb=1` — serves free-capture files with same thumbnail support
- Color fix: `capture_still()` flips BGR→RGB before returning (libcamera delivers BGR despite RGB888 label on Pi 5)

### Frontend (`capture/app/static/`)
- Dark instrument-style UI — GitHub dark palette, monospace readouts, no framework
- Status bar: connection dot, camera state, AE lock with exposure values, disk free (color-coded)
- MJPEG live preview with focus score updated every 500ms
- AE lock/unlock — locked values shown in header chip and preview overlay
- Magnifier on hold (mousedown / spacebar) — fetches `/magnifier.jpg` every 500ms, full-screen overlay
- Free Still capture with filename feedback + thumbnail strip
- Free Video capture with duration input and live countdown progress bar
- Session sidebar: create session (motility/survival with assay_config), expand, add plates (pre-fills from last plate + auto-increments plate_number), select active plate
- Session capture panel adapts to assay mode: motility recording with progress / survival single / quadrant 2×2 grid (captured quadrants turn green)
- Thumbnail strip: last 8 captures in current context; click → full-size modal
- Token prompt on load; `?token=` URL param captured and URL-cleaned for Phase 4 launcher
- Polling pauses when tab is hidden

## Known polish items (deferred to next session)

### 1. Video thumbnails and playback
- Video files in the thumbnail strip show a play icon but don't open to a playable video in the modal (modal uses `<img>`, not `<video>`)
- Need to investigate: are MP4s reliably remuxed by ffmpeg? Does `FileResponse` set `Content-Type: video/mp4`?
- Consider generating a thumbnail from the first frame via `ffmpeg -ss 0 -frames:v 1`
- Modal should detect video files and render a `<video>` element instead of `<img>`

### 2. Magnifier latency
- Currently polls `/magnifier.jpg` every 500ms; each call does a fresh full-res capture + 600×600 crop + JPEG encode → feels choppy
- Options: tighter polling interval, server-side caching of the latest magnifier crop on a background thread, smaller crop window, or a dedicated MJPEG sub-stream cropped to center (same architecture as the preview stream)

## Performance (Phase 2 baseline, unchanged)

| Operation | Wall clock |
|-----------|-----------|
| `POST /capture/free/still` | ~0.42s |
| `POST /sessions/.../capture` (survival, single) | ~0.38s |
| `POST /sessions/.../capture` (survival, quadrant) | ~0.38s |

## Next: Phase 3 polish → then Phase 4
