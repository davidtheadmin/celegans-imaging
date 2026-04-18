# Phase 2 Status — Complete

## Performance (measured on Pi, 2026-04-18)

| Operation | Wall clock |
|-----------|-----------|
| `POST /capture/free/still` | ~0.42s |
| `POST /sessions/.../capture` (survival, single) | ~0.38s |
| `POST /sessions/.../capture` (survival, quadrant) | ~0.38s |

Breakdown: `capture_array("main")` = 0.106s, `save_jpeg()` = 0.132s.
Survival is within 1.0× free-capture still — well under the 1.5× target.

## What's working

- `GET /preview.mjpg` — MJPEG stream, ~10-20 fps
- `GET /focus` — Laplacian variance score
- `POST /camera/ae/lock` / `unlock` / `GET /camera/exposure`
- `GET /status`
- `POST /capture/free/still` — ~0.42s
- `POST /capture/free/video` — H.264 + ffmpeg MP4 wrap
- `POST /sessions/.../capture` (motility) — video, ~5s for 5s clip
- `POST /sessions/.../capture` (survival, single still) — ~0.38s ✓
- `POST /sessions/.../capture` (survival, quadrant NE/NW/SE/SW) — ~0.38s each ✓
- Quadrant guard: missing `quadrant` on `quadrants:true` session → 400 ✓

## asyncio.to_thread coverage

All camera and disk I/O runs off the event loop:

| Endpoint | Off-thread function |
|----------|---------------------|
| `POST /capture/free/still` | `capture_ops.free_still` |
| `POST /capture/free/video` | `capture_ops.free_video` |
| `POST /sessions/.../capture` (motility) | `capture_ops.plate_motility` |
| `POST /sessions/.../capture` (survival) | `capture_ops.plate_survival` |
| `GET /magnifier.jpg` | `_capture_magnifier` (capture + PIL encode) |
| `POST /camera/ae/lock` | `camera_manager.lock_ae` |
| `POST /camera/ae/unlock` | `camera_manager.unlock_ae` |

## Root-cause note

The 3m48s survival hang reported in the previous session was on the Phase 1
skeleton (commit 5c2e646). Phase 2 was never actually deployed to the Pi due
to `main` tracking the deleted `origin/master` branch — silent no-op on every
`git pull`. Fixed by resetting upstream to `origin/main`. Phase 2 code paths
for free/still and plate/survival are identical through `capture_still()` and
`save_jpeg()`; both are fast once deployed.

## Next: Phase 3

Flat-field correction pipeline, exposure calibration endpoint.
