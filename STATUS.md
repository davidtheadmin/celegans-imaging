# Phase 2 Status

## What's working

- `GET /preview.mjpg` — MJPEG stream, ~10-20 fps, correct colors (YUV420 I420 path confirmed)
- `GET /focus` — Laplacian variance score, live updates
- `POST /camera/ae/lock` / `unlock` / `GET /camera/exposure` — AE lock reads and freezes real sensor values
- `GET /status` — includes `camera_ready` and `ae_locked`
- `POST /capture/free/still` — full-res still, fast
- `POST /capture/free/video` — H.264 recording, ffmpeg MP4 wrap confirmed working
- `POST /sessions/.../capture` (motility) — video recording, correct plate folder, ~5s for 5s clip
- Quadrant guard: `POST .../capture` without `quadrant` on a `quadrants:true` session → 400 ✓

## What's broken

### Survival plate capture is pathologically slow (3m48s+)

`POST /sessions/{session_id}/plates/{plate_id}/capture` on a survival session hangs for
at least 3m48s before any response. The endpoint never errored — it just never returned
within a reasonable time. `Ctrl+C` was needed to abort.

For comparison, `POST /capture/free/still` (same camera operation, different destination)
completes quickly. Something specific to the survival code path is doing excessive work.

## Hypotheses (in rough likelihood order)

1. **Camera reconfiguration** — `plate_survival` in `capture_ops.py` calls
   `cam_mgr.capture_still()` → `cam.capture_array("main")`. In a video configuration,
   picamera2 may be switching sensor modes internally before returning a full-res frame,
   taking minutes rather than milliseconds. Free-capture still uses the same call, but
   something in the call stack or camera state may differ.

2. **Asyncio event loop blocking** — `capture_ops.plate_survival` is called via
   `asyncio.to_thread()` in `plate_capture.py`, same as free capture. But if the thread
   dispatching is not working correctly for this path, the coroutine could be blocking
   the event loop and starving itself.

3. **Redundant camera start/stop** — unlikely given the code, but worth verifying that
   `CameraManager.start()` is not being called again mid-request.

4. **Flat-field correction applied unintentionally** — `apply_ff` defaults to `False` and
   `load_flat()` raises 400 immediately if the master flat is missing, so this would fail
   fast rather than hang. Unlikely to be the cause.

## Concrete next steps

1. **Diff the two code paths** — trace `POST /capture/free/still` vs
   `POST /sessions/.../capture` (survival) through:
   - `capture/app/routers/free_capture.py` → `capture_ops.free_still()`
   - `capture/app/routers/plate_capture.py` → `capture_ops.plate_survival()`
   Both call `cam_mgr.capture_still()` → `cam.capture_array("main")`. Confirm they are
   genuinely identical in the camera call.

2. **Add timing logs** inside `CameraManager.capture_still()` to measure how long
   `cam.capture_array("main")` actually takes. If it's minutes, the issue is in
   picamera2 / the video config, not in the application layer.

3. **Live inspection if needed** — `ps aux` + `py-spy` or `/proc/<pid>/wchan` on the Pi
   to see where the process is blocked during a survival capture hang.

4. **Possible fix** — if `capture_array("main")` is slow in video config for on-demand
   stills, switch to `switch_mode_and_capture_array()` with a dedicated still config,
   or use a separate `capture_request()` approach that doesn't stall.

5. **Re-run full survival test suite** after fix:
   - Single still (no quadrants)
   - Four-quadrant mode (NE/NW/SE/SW)
   - Quadrant guard (missing quadrant → 400)

## Relevant files

- `capture/app/capture_ops.py` — `free_still()`, `plate_survival()`, `plate_motility()`
- `capture/app/routers/plate_capture.py` — survival/motility dispatch
- `capture/app/routers/free_capture.py` — free-capture endpoints
- `capture/app/camera.py` — `CameraManager.capture_still()`, `_preview_loop()`
