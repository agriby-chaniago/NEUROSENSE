"""
sensors/camera_reader.py  –  Arducam / Raspberry Pi Camera reader.

Backend priority
----------------
1. picamera2  – native CSI ribbon cable / Arducam CSI (RPi OS Bullseye+)
2. OpenCV     – USB Arducam or any V4L2 /dev/videoX device

Thread-safe: a daemon thread captures frames continuously; the latest
JPEG bytes are kept in memory and served on demand by the Flask MJPEG route.

Usage
-----
    cam = CameraReader()
    cam.start()
    frame = cam.get_frame()   # bytes | None
    cam.stop()
"""

import io
import logging
import threading
import time
from typing import Optional

import config

logger = logging.getLogger(__name__)

# How long (s) to wait between retries if the camera stream crashes
_RESTART_DELAY_S = 3.0


class CameraReader:
    """
    Background-thread camera reader.

    Call start() once; the thread captures frames until stop() is called.
    get_frame() returns the latest JPEG as bytes, or None if not yet ready.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._backend: Optional[str] = None   # "picamera2" | "opencv"
        self._error: Optional[str] = None     # last fatal error message

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialise camera hardware and start the capture thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="camera",
            daemon=True,
        )
        self._thread.start()
        logger.info("CameraReader: capture thread started")

    def stop(self) -> None:
        """Signal the capture thread to stop and wait for it."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=6.0)
        logger.info("CameraReader: stopped")

    # ── Frame access ─────────────────────────────────────────────────────

    def get_frame(self) -> Optional[bytes]:
        """Return latest JPEG frame bytes, or None if not yet available."""
        with self._lock:
            return self._frame

    @property
    def backend(self) -> Optional[str]:
        """Which backend is active: 'picamera2', 'opencv', or None if failed."""
        return self._backend

    @property
    def ready(self) -> bool:
        """True once the first frame has been captured."""
        with self._lock:
            return self._frame is not None

    def health(self) -> dict:
        return {
            "sensor":  "camera",
            "ok":      self._backend is not None and self._error is None,
            "backend": self._backend,
            "error":   self._error,
        }

    # ── Main capture loop ─────────────────────────────────────────────────

    def _capture_loop(self):
        """Try picamera2 first; fall back to OpenCV on failure."""
        # ── Attempt 1: picamera2 ─────────────────────────────────────────
        try:
            self._loop_picamera2()
            return   # clean exit (stop() was called)
        except ModuleNotFoundError:
            logger.info("CameraReader: picamera2 not installed — trying OpenCV")
        except Exception as exc:
            if not self._running:
                return
            logger.warning("CameraReader: picamera2 failed (%s) — trying OpenCV", exc)

        # ── Attempt 2: OpenCV ─────────────────────────────────────────────
        try:
            self._loop_opencv()
        except Exception as exc:
            if self._running:
                self._error = str(exc)
                logger.error("CameraReader: OpenCV also failed: %s", exc)

    # ── picamera2 backend ─────────────────────────────────────────────────

    def _loop_picamera2(self):
        from picamera2 import Picamera2  # type: ignore

        cam = Picamera2()

        # Build config: size + optional rotation via Transform
        video_cfg = cam.create_video_configuration(
            main={
                "size":   (config.CAMERA_WIDTH, config.CAMERA_HEIGHT),
                "format": "RGB888",
            },
            controls={
                "FrameDurationLimits": (
                    int(1_000_000 / config.CAMERA_FRAMERATE),
                    int(1_000_000 / config.CAMERA_FRAMERATE),
                )
            },
        )

        # Apply rotation if configured
        rotation = getattr(config, "CAMERA_ROTATION", 0)
        if rotation != 0:
            try:
                from libcamera import Transform  # type: ignore
                transform_map = {
                    90:  Transform(rotation=90),
                    180: Transform(hflip=1, vflip=1),
                    270: Transform(rotation=270),
                }
                if rotation in transform_map:
                    video_cfg["transform"] = transform_map[rotation]
            except ImportError:
                pass

        cam.configure(video_cfg)
        cam.start()

        self._backend = "picamera2"
        self._error = None
        logger.info(
            "CameraReader: picamera2 started (%dx%d @ %d fps)",
            config.CAMERA_WIDTH, config.CAMERA_HEIGHT, config.CAMERA_FRAMERATE,
        )

        try:
            from PIL import Image  # type: ignore

            while self._running:
                # capture_array returns an np.ndarray (RGB888)
                array = cam.capture_array("main")
                img = Image.fromarray(array)

                # Software rotation fallback (if libcamera Transform unavailable)
                rotation = getattr(config, "CAMERA_ROTATION", 0)
                if rotation == 90:
                    img = img.rotate(-90, expand=True)
                elif rotation == 180:
                    img = img.rotate(180, expand=True)
                elif rotation == 270:
                    img = img.rotate(90, expand=True)

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=config.CAMERA_JPEG_QUALITY)
                jpeg_bytes = buf.getvalue()

                with self._lock:
                    self._frame = jpeg_bytes

        finally:
            cam.stop()
            cam.close()

    # ── OpenCV backend ────────────────────────────────────────────────────

    def _loop_opencv(self):
        import cv2  # type: ignore

        device = getattr(config, "CAMERA_DEVICE_INDEX", 0)
        cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            raise RuntimeError(
                f"OpenCV: cannot open camera at index/path {device!r}"
            )

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  config.CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS,          config.CAMERA_FRAMERATE)

        self._backend = "opencv"
        self._error = None
        logger.info(
            "CameraReader: OpenCV started (device=%r, %dx%d @ %d fps)",
            device, config.CAMERA_WIDTH, config.CAMERA_HEIGHT, config.CAMERA_FRAMERATE,
        )

        rotation_map = {
            90:  cv2.ROTATE_90_CLOCKWISE,
            180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE,
        }
        rotation = getattr(config, "CAMERA_ROTATION", 0)
        rotate_code = rotation_map.get(rotation)

        interval = 1.0 / max(1, config.CAMERA_FRAMERATE)

        try:
            while self._running:
                t0 = time.monotonic()
                ok, frame = cap.read()
                if ok:
                    if rotate_code is not None:
                        frame = cv2.rotate(frame, rotate_code)

                    _, buf = cv2.imencode(
                        ".jpg", frame,
                        [cv2.IMWRITE_JPEG_QUALITY, config.CAMERA_JPEG_QUALITY],
                    )
                    with self._lock:
                        self._frame = buf.tobytes()

                elapsed = time.monotonic() - t0
                wait = interval - elapsed
                if wait > 0:
                    time.sleep(wait)
        finally:
            cap.release()
