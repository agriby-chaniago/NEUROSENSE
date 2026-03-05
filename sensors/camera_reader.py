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
        # threading.Condition wraps a lock; notify_all() wakes the MJPEG
        # generator the instant a new frame arrives — no polling needed.
        self._cond = threading.Condition()
        self._frame: Optional[bytes] = None
        self._frame_seq: int = 0   # increments every new frame
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
        with self._cond:
            self._cond.notify_all()   # unblock any waiting get_new_frame()
        if self._thread is not None:
            self._thread.join(timeout=6.0)
        logger.info("CameraReader: stopped")

    # ── Frame access ─────────────────────────────────────────────────────

    def get_frame(self) -> Optional[bytes]:
        """Return latest JPEG frame bytes, or None if not yet available."""
        with self._cond:
            return self._frame

    def get_new_frame(self, timeout: float = 0.25) -> Optional[bytes]:
        """
        Block until a NEW frame is captured (or timeout).
        Used by the MJPEG generator to yield frames with zero polling delay.
        """
        with self._cond:
            seq = self._frame_seq
            self._cond.wait_for(
                lambda: self._frame_seq != seq or not self._running,
                timeout=timeout,
            )
            return self._frame

    @property
    def backend(self) -> Optional[str]:
        """Which backend is active: 'picamera2', 'opencv', or None if failed."""
        return self._backend

    @property
    def ready(self) -> bool:
        """True once the first frame has been captured."""
        with self._cond:
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

        # Check that at least one camera is visible to libcamera before opening
        available = Picamera2.global_camera_info()
        if not available:
            raise RuntimeError(
                "No cameras detected by libcamera. "
                "Run: libcamera-hello --list-cameras\n"
                "If empty, enable the camera overlay:\n"
                "  sudo raspi-config → Interface Options → Camera\n"
                "  OR add 'camera_auto_detect=1' to /boot/firmware/config.txt "
                "then reboot."
            )
        logger.info("CameraReader: libcamera detected %d camera(s): %s",
                    len(available), [c.get('Model', '?') for c in available])

        cam = Picamera2()

        # Build config: size + rotation
        frame_us = int(1_000_000 / max(1, config.CAMERA_FRAMERATE))
        video_cfg = cam.create_video_configuration(
            main={
                # Request RGB888.  Arducam 64MP ISP delivers RGB byte order.
                # If skin appears blue, set CAMERA_SWAP_RB = True in config.py.
                "size":   (config.CAMERA_WIDTH, config.CAMERA_HEIGHT),
                "format": "RGB888",
            },
            controls={
                # Allow dynamic range: libcamera picks the fastest achievable
                # rate up to CAMERA_FRAMERATE. Avoids hard-locking to one fps.
                "FrameDurationLimits": (frame_us, frame_us * 3),
                "AwbEnable": True,
                "AeEnable":  True,
                "Sharpness": getattr(config, "CAMERA_SHARPNESS", 2.0),
            },
            buffer_count=4,
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

        # ── Autofocus (Arducam 64MP AF / OV64A40) ────────────────────────
        # Must be called AFTER cam.start(). Integer constants used to avoid
        # libcamera enum import issues on some Arducam driver versions.
        #   AfMode: 0=Manual, 1=Auto(one-shot), 2=Continuous
        #   AfSpeed: 0=Normal, 1=Fast
        # Note: AfTrigger is NOT set — OV64A40 via PiSP rejects it before the
        # AF algorithm initialises; Continuous mode auto-starts scanning.
        if getattr(config, "CAMERA_AUTOFOCUS", False):
            try:
                cam.set_controls({"AfMode": 2, "AfSpeed": 1})
                logger.info("CameraReader: continuous autofocus enabled (Arducam 64MP AF)")
            except Exception as af_exc:
                logger.warning(
                    "CameraReader: could not enable AF: %s", af_exc,
                )

        self._backend = "picamera2"
        self._error = None
        logger.info(
            "CameraReader: picamera2 started (%dx%d @ %d fps)",
            config.CAMERA_WIDTH, config.CAMERA_HEIGHT, config.CAMERA_FRAMERATE,
        )

        try:
            # Import cv2 once — much faster JPEG encoding than PIL
            try:
                import cv2 as _cv2
                _use_cv2 = True
            except ImportError:
                from PIL import Image  # type: ignore
                _use_cv2 = False

            while self._running:
                array = cam.capture_array("main")

                # ── Colour channel handling ───────────────────────────────
                if array.ndim == 3 and array.shape[2] == 4:
                    # XBGR/BGRA — strip alpha, then R↔B swap
                    array = array[:, :, :3][:, :, ::-1].copy()
                elif getattr(config, "CAMERA_SWAP_RB", False):
                    array = array[:, :, ::-1].copy()
                # else: RGB correct as-is

                # ── Software rotation ──────────────────────────────────
                rotation = getattr(config, "CAMERA_ROTATION", 0)

                # ── JPEG encoding ─────────────────────────────────────
                if _use_cv2:
                    # cv2 expects BGR; array is RGB — flip channels
                    bgr = array[:, :, ::-1]
                    if rotation == 90:
                        bgr = _cv2.rotate(bgr, _cv2.ROTATE_90_CLOCKWISE)
                    elif rotation == 180:
                        bgr = _cv2.rotate(bgr, _cv2.ROTATE_180)
                    elif rotation == 270:
                        bgr = _cv2.rotate(bgr, _cv2.ROTATE_90_COUNTERCLOCKWISE)
                    _, buf = _cv2.imencode(
                        ".jpg", bgr,
                        [_cv2.IMWRITE_JPEG_QUALITY, config.CAMERA_JPEG_QUALITY],
                    )
                    jpeg_bytes = buf.tobytes()
                else:
                    img = Image.fromarray(array, mode="RGB")
                    if rotation == 90:
                        img = img.rotate(-90, expand=True)
                    elif rotation == 180:
                        img = img.rotate(180, expand=True)
                    elif rotation == 270:
                        img = img.rotate(90, expand=True)
                    _buf = io.BytesIO()
                    img.save(_buf, format="JPEG", quality=config.CAMERA_JPEG_QUALITY)
                    jpeg_bytes = _buf.getvalue()

                with self._cond:
                    self._frame = jpeg_bytes
                    self._frame_seq += 1
                    self._cond.notify_all()

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
        # Request camera-side auto-white-balance and auto-exposure
        cap.set(cv2.CAP_PROP_AUTO_WB,          1)
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE,    3)   # 3 = aperture priority (auto)
        # Sharpness: 0=off, higher = sharper (range is driver-dependent)
        sharpness = getattr(config, "CAMERA_SHARPNESS", 1.5)
        cap.set(cv2.CAP_PROP_SHARPNESS, sharpness)

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
                ok, frame = cap.read()
                if ok:
                    if rotate_code is not None:
                        frame = cv2.rotate(frame, rotate_code)

                    _, buf = cv2.imencode(
                        ".jpg", frame,
                        [cv2.IMWRITE_JPEG_QUALITY, config.CAMERA_JPEG_QUALITY],
                    )
                    with self._cond:
                        self._frame = buf.tobytes()
                        self._frame_seq += 1
                        self._cond.notify_all()
        finally:
            cap.release()
