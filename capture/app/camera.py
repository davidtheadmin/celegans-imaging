import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput

from .config import settings

log = logging.getLogger(__name__)

FULL_W, FULL_H = 4056, 3040
PREVIEW_W, PREVIEW_H = 1280, 960
DEFAULT_VIDEO_FPS = 30  # fallback; IMX708 at 4056×3040 delivers ~10fps


class CameraManager:
    def __init__(self):
        self._cam: Optional[Picamera2] = None
        self._capture_lock = threading.Lock()
        self._preview_thread: Optional[threading.Thread] = None
        self._running = False

        self._frame_lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_lores: Optional[np.ndarray] = None  # RGB uint8

        self._ae_locked = False
        self._exposure_us: Optional[int] = None
        self._analogue_gain: Optional[float] = None
        self._recording = False
        self._encoder: Optional[H264Encoder] = None
        self._video_fps: float = DEFAULT_VIDEO_FPS

    def start(self) -> None:
        try:
            cam = Picamera2()
            config = cam.create_video_configuration(
                main={"size": (FULL_W, FULL_H), "format": "RGB888"},
                lores={"size": (PREVIEW_W, PREVIEW_H), "format": "YUV420"},
            )
            cam.configure(config)
            cam.start()
            # Cap AE shutter via FrameDurationLimits (min_us, max_us).
            # ExposureTimeMax is not a valid picamera2 control; FrameDurationLimits
            # is the correct mechanism — it constrains both shutter and frame time.
            min_frame_us = 1_000
            cam.set_controls({"FrameDurationLimits": (min_frame_us, settings.MAX_AUTO_SHUTTER_US)})
            log.info(
                "Camera: FrameDurationLimits set to (%d, %d) µs — AE shutter capped at %.0f ms",
                min_frame_us, settings.MAX_AUTO_SHUTTER_US, settings.MAX_AUTO_SHUTTER_US / 1000,
            )
            time.sleep(2.0)  # AE / AWB settle

            # Measure actual fps while single-threaded (preview not yet started).
            # We read FrameDuration here so we never need capture_request() at
            # recording time — that call races with capture_array("lores") in the
            # preview loop and deadlocks on picamera2's internal libcamera queue.
            fps = DEFAULT_VIDEO_FPS
            try:
                req = cam.capture_request()
                try:
                    meta = req.get_metadata()
                finally:
                    req.release()
                fd_us = meta.get("FrameDuration", 0)
                if fd_us and fd_us > 0:
                    fps = 1_000_000.0 / fd_us
                    log.info("Camera: measured %.2f fps (FrameDuration=%d µs)", fps, fd_us)
            except Exception as exc:
                log.warning("Camera: fps measurement failed (%s) — using fallback %.0f fps", exc, fps)

            self._video_fps = fps
            self._cam = cam
            self._running = True
            self._preview_thread = threading.Thread(
                target=self._preview_loop, daemon=True, name="preview"
            )
            self._preview_thread.start()
            log.info("Camera started (%dx%d main, %dx%d lores)",
                     FULL_W, FULL_H, PREVIEW_W, PREVIEW_H)
        except Exception as exc:
            log.error("Camera failed to start: %s", exc)

    def stop(self) -> None:
        self._running = False
        if self._preview_thread:
            self._preview_thread.join(timeout=5)
        if self._cam:
            try:
                self._cam.stop()
            except Exception:
                pass
            self._cam.close()
            self._cam = None

    def _preview_loop(self) -> None:
        while self._running:
            try:
                # No _capture_lock here: the preview is a passive lores consumer.
                # Holding _capture_lock during capture_array("lores") would block
                # start_video_recording() from acquiring it — and start_recording()
                # is what "kicks" picamera2 back into delivering lores frames after
                # a recording has stopped.  The lores stream is independent of the
                # main-stream state-changing operations protected by _capture_lock.
                # YUV420 I420 -> BGR for JPEG encoding.
                # If preview looks green, change to COLOR_YUV2BGR_NV12.
                yuv = self._cam.capture_array("lores")
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                ok, jpeg_arr = cv2.imencode(
                    ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 70]
                )
                if not ok:
                    continue
                rgb = bgr[:, :, ::-1].copy()
                with self._frame_lock:
                    self._latest_jpeg = jpeg_arr.tobytes()
                    self._latest_lores = rgb
            except Exception:
                if self._running:
                    time.sleep(0.05)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._cam is not None and self._running

    @property
    def ae_locked(self) -> bool:
        return self._ae_locked

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def video_fps(self) -> float:
        """Actual camera fps measured at startup. Safe to read from any thread."""
        return self._video_fps

    # ------------------------------------------------------------------
    # Frame access
    # ------------------------------------------------------------------

    def get_latest_jpeg(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._latest_jpeg

    def get_latest_lores(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            return self._latest_lores.copy() if self._latest_lores is not None else None

    # ------------------------------------------------------------------
    # AE lock / unlock
    # ------------------------------------------------------------------

    def lock_ae(self) -> dict:
        with self._capture_lock:
            req = self._cam.capture_request()
            try:
                meta = req.get_metadata()
            finally:
                req.release()
            exp = int(meta.get("ExposureTime", 10_000))
            gain = float(meta.get("AnalogueGain", 1.0))
            self._cam.set_controls({
                "AeEnable": False,
                "ExposureTime": exp,
                "AnalogueGain": gain,
            })
            self._ae_locked = True
            self._exposure_us = exp
            self._analogue_gain = gain
            return {
                "locked": True,
                "exposure_us": exp,
                "analogue_gain": gain,
                "locked_at": datetime.now(timezone.utc).isoformat(),
            }

    def unlock_ae(self) -> None:
        with self._capture_lock:
            self._cam.set_controls({"AeEnable": True})
            self._ae_locked = False
            self._exposure_us = None
            self._analogue_gain = None

    def get_exposure_state(self) -> dict:
        return {
            "locked": self._ae_locked,
            "exposure_us": self._exposure_us,
            "analogue_gain": self._analogue_gain,
        }

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_still(self) -> np.ndarray:
        """Return full-res RGB array. Raises HTTPException(409) if recording."""
        from fastapi import HTTPException
        t0 = time.perf_counter()
        with self._capture_lock:
            t1 = time.perf_counter()
            if self._recording:
                raise HTTPException(409, "Video recording in progress")
            arr = self._cam.capture_array("main")
            t2 = time.perf_counter()
        log.debug("[TIMING] capture_still: lock_wait=%.3fs capture_array=%.3fs",
                  t1 - t0, t2 - t1)
        return arr[..., ::-1].copy()  # libcamera delivers BGR despite RGB888 label on Pi 5

    def start_video_recording(self, path: Path, bitrate_bps: int) -> None:
        from fastapi import HTTPException
        log.debug("start_video_recording: waiting for lock")
        t0 = time.perf_counter()
        with self._capture_lock:
            log.debug("start_video_recording: lock acquired after %.3fs", time.perf_counter() - t0)
            if self._recording:
                raise HTTPException(409, "Already recording")
            encoder = H264Encoder(bitrate=bitrate_bps)
            log.debug("start_video_recording: calling cam.start_recording")
            t1 = time.perf_counter()
            self._cam.start_recording(encoder, FileOutput(str(path)), name="main")
            log.debug("start_video_recording: cam.start_recording returned in %.3fs", time.perf_counter() - t1)
            self._encoder = encoder
            self._recording = True
            log.info("Video recording started -> %s", path)

    def stop_video_recording(self) -> None:
        log.debug("stop_video_recording: waiting for lock")
        t0 = time.perf_counter()
        with self._capture_lock:
            log.debug("stop_video_recording: lock acquired after %.3fs", time.perf_counter() - t0)
            t1 = time.perf_counter()
            self._cam.stop_encoder()
            log.debug("stop_video_recording: cam.stop_recording returned in %.3fs", time.perf_counter() - t1)
            self._encoder = None  # release encoder reference; prevents GC race on next start_recording
            self._recording = False
            log.info("Video recording stopped")


camera_manager = CameraManager()
