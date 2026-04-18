# Phase 2 Status

## What's working

- `GET /preview.mjpg` — MJPEG stream, ~10-20 fps, correct colors (YUV420 I420 path confirmed)
- `GET /focus` — Laplacian variance score, live updates
- `POST /camera/ae/lock` / `unlock` / `GET /camera/exposure` — AE lock reads and freezes real sensor values
- `GET /status` — includes `camera_ready` and `ae_locked`
- `POST /capture/free/still` — full-res still, ~0.42s wall clock (capture_array=0.106s, save_jpeg=0.132s)
- `POST /capture/free/video` — H.264 recording, ffmpeg MP4 wrap confirmed working
- `POST /sessions/.../capture` (motility) — video recording, correct plate folder, ~5s for 5s clip
- `POST /sessions/.../capture` (survival) — fast, same code path as free/still
- Quadrant guard: `POST .../capture` without `quadrant` on a `quadrants:true` session → 400 ✓

## asyncio.to_thread coverage

All endpoints that do camera I/O or disk I/O run in `asyncio.to_thread()`:

| Endpoint | Handler |
|----------|---------|
| `POST /capture/free/still` | `capture_ops.free_still` |
| `POST /capture/free/video` | `capture_ops.free_video` |
| `POST /sessions/.../capture` (motility) | `capture_ops.plate_motility` |
| `POST /sessions/.../capture` (survival) | `capture_ops.plate_survival` |
| `GET /magnifier.jpg` | `_capture_magnifier` (capture + PIL encode) |
| `POST /camera/ae/lock` | `camera_manager.lock_ae` |
| `POST /camera/ae/unlock` | `camera_manager.unlock_ae` |

## Phase 2 complete

All Phase 2 scope delivered and confirmed on Pi:

- Camera integration: preview, AE lock/unlock, focus score, magnifier
- Full-res still capture (free and plate/survival): ~0.42s
- Timelapse/video capture (free and plate/motility)
- All capture endpoints use `asyncio.to_thread()` for I/O
- Survival test suite: pending final run (see next steps)

## Next: Phase 3

Flat-field correction pipeline, exposure calibration endpoint.
