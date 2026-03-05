"""
experiments/session_manager.py  –  Manages experiment recording sessions.

A session = one respondent × one condition × a fixed recording duration.

Directory layout per session
-----------------------------
    data/sessions/{session_id}/
        metadata.json       – session info (respondent, condition, timestamps)
        sensor_raw.csv      – aligned sensor readings (~10 Hz)
        frames/             – individual JPEG frames (000000.jpg, 000001.jpg …)
        video.mp4           – assembled from frames after session stops

Recording threads
-----------------
  sensor loop   : polls SensorManager.get_latest() every 100 ms → sensor_raw.csv
  video loop    : calls CameraReader.get_new_frame() → frames/ → assemble MP4
  timer loop    : sets stop_event after duration_sec → triggers finalize
"""

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Sensor fields recorded per row in session sensor_raw.csv
SESSION_SENSOR_FIELDS = [
    "timestamp_ms",
    "heart_rate_bpm",
    "spo2_percent",
    "temperature_celsius",
    "gsr_raw_adc",
    "gsr_conductance_us",
]

CONDITION_LABELS = getattr(
    config, "EXPERIMENT_CONDITIONS",
    ["normal", "anxiety", "stress", "depression"],
)


class SessionManager:
    """
    Controls data collection sessions for the NeuroSense experiment.

    Inject sensor_manager and camera_reader at construction time; both
    are optional — missing components are silently skipped (useful for
    testing without hardware).
    """

    def __init__(self, sensor_manager=None, camera_reader=None):
        self._sensor_manager = sensor_manager
        self._camera_reader  = camera_reader
        self._lock = threading.Lock()

        self._active: Optional[dict] = None   # running session state
        self._threads: list[threading.Thread] = []

        sessions_dir = getattr(
            config, "EXPERIMENT_SESSIONS_DIR",
            os.path.join(config.DATA_DIR, "sessions"),
        )
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

    # ─── Session lifecycle ────────────────────────────────────────────────

    def start_session(
        self,
        respondent_id: str,
        condition_label: str,
        duration_sec: int = None,
    ) -> dict:
        """
        Start a new recording session.

        Returns
        -------
        dict : initial metadata for the session

        Raises
        ------
        RuntimeError  – if another session is already active
        ValueError    – if condition_label is unknown
        """
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    "Another session is already active. "
                    f"Stop session {self._active['metadata']['session_id']} first."
                )

        if condition_label not in CONDITION_LABELS:
            raise ValueError(
                f"Unknown condition '{condition_label}'. "
                f"Valid values: {CONDITION_LABELS}"
            )

        if duration_sec is None:
            duration_sec = int(
                getattr(config, "EXPERIMENT_SESSION_DURATION_S", 60)
            )

        session_id  = self._next_session_id()
        started_at  = datetime.now(timezone.utc)

        session_dir = self._sessions_dir / session_id
        frames_dir  = session_dir / "frames"
        session_dir.mkdir(parents=True, exist_ok=True)
        frames_dir.mkdir(exist_ok=True)

        metadata = {
            "session_id":      session_id,
            "respondent_id":   respondent_id,
            "condition_label": condition_label,
            "duration_sec":    duration_sec,
            "started_at_utc":  started_at.isoformat(),
            "stopped_at_utc":  None,
            "status":          "recording",
            "frame_count":     0,
            "sensor_rows":     0,
        }
        self._save_json(session_dir / "metadata.json", metadata)

        stop_event = threading.Event()

        with self._lock:
            self._active = {
                "metadata":    metadata,
                "session_dir": session_dir,
                "frames_dir":  frames_dir,
                "stop_event":  stop_event,
                "start_mono":  time.monotonic(),
            }

        # Launch parallel recording threads
        t_sensor = threading.Thread(
            target=self._sensor_loop,
            args=(session_dir, stop_event, metadata),
            name="session-sensor",
            daemon=True,
        )
        t_video = threading.Thread(
            target=self._video_loop,
            args=(session_dir, frames_dir, stop_event, metadata),
            name="session-video",
            daemon=True,
        )
        t_timer = threading.Thread(
            target=self._timer_loop,
            args=(duration_sec, stop_event),
            name="session-timer",
            daemon=True,
        )

        self._threads = [t_sensor, t_video, t_timer]
        for t in self._threads:
            t.start()

        logger.info(
            "Session %s started — respondent=%s  condition=%s  duration=%ds",
            session_id, respondent_id, condition_label, duration_sec,
        )
        return metadata

    def stop_session(self) -> Optional[dict]:
        """Manually stop the active session before its timer expires."""
        with self._lock:
            active = self._active

        if active is None:
            return None

        active["stop_event"].set()
        for t in self._threads:
            t.join(timeout=15.0)

        return self._finalize_session()

    def get_active_session(self) -> Optional[dict]:
        """
        Return a snapshot of the active session metadata with elapsed_sec,
        or None if no session is running.
        """
        with self._lock:
            if self._active is None:
                return None
            meta = dict(self._active["metadata"])
            meta["elapsed_sec"] = int(
                time.monotonic() - self._active["start_mono"]
            )
        return meta

    def list_sessions(self) -> list[dict]:
        """Return metadata dicts for all sessions, newest first."""
        result = []
        for d in sorted(self._sessions_dir.iterdir(), reverse=True):
            meta_file = d / "metadata.json"
            if meta_file.exists():
                try:
                    with open(meta_file, encoding="utf-8") as f:
                        result.append(json.load(f))
                except Exception:
                    pass
        return result

    def get_session(self, session_id: str) -> Optional[dict]:
        """Return metadata for a specific session, or None."""
        meta_file = self._sessions_dir / session_id / "metadata.json"
        if not meta_file.exists():
            return None
        with open(meta_file, encoding="utf-8") as f:
            return json.load(f)

    # ─── Recording threads ────────────────────────────────────────────────

    def _sensor_loop(
        self,
        session_dir: Path,
        stop_event: threading.Event,
        metadata: dict,
    ):
        """Record sensor readings to sensor_raw.csv at ~10 Hz."""
        if self._sensor_manager is None:
            logger.warning("SessionManager: no sensor_manager — sensor recording skipped")
            return

        csv_path  = session_dir / "sensor_raw.csv"
        start_wall = time.time()
        row_count = 0

        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=SESSION_SENSOR_FIELDS, extrasaction="ignore"
            )
            writer.writeheader()

            while not stop_event.is_set():
                t0   = time.monotonic()
                data = self._sensor_manager.get_latest()
                row  = {
                    "timestamp_ms":       int((time.time() - start_wall) * 1000),
                    "heart_rate_bpm":     data.get("heart_rate_bpm"),
                    "spo2_percent":       data.get("spo2_percent"),
                    "temperature_celsius": data.get("temperature_celsius"),
                    "gsr_raw_adc":        data.get("gsr_raw_adc"),
                    "gsr_conductance_us": data.get("gsr_conductance_us"),
                }
                writer.writerow(row)
                fh.flush()
                row_count += 1

                elapsed = time.monotonic() - t0
                stop_event.wait(timeout=max(0.0, 0.1 - elapsed))  # ~10 Hz

        with self._lock:
            if self._active:
                self._active["metadata"]["sensor_rows"] = row_count

        logger.info("Session %s sensor loop done: %d rows", metadata["session_id"], row_count)

    def _video_loop(
        self,
        session_dir: Path,
        frames_dir: Path,
        stop_event: threading.Event,
        metadata: dict,
    ):
        """Save JPEG frames, then assemble MP4 after stop."""
        if self._camera_reader is None:
            logger.warning("SessionManager: no camera_reader — video recording skipped")
            return

        frame_count = 0

        while not stop_event.is_set():
            frame = self._camera_reader.get_new_frame(timeout=0.5)
            if frame is None:
                continue
            (frames_dir / f"{frame_count:06d}.jpg").write_bytes(frame)
            frame_count += 1

        with self._lock:
            if self._active:
                self._active["metadata"]["frame_count"] = frame_count

        logger.info("Session %s video loop done: %d frames", metadata["session_id"], frame_count)
        self._assemble_mp4(session_dir, frames_dir, frame_count)

    def _timer_loop(self, duration_sec: int, stop_event: threading.Event):
        """Signal stop after duration_sec, then trigger finalize."""
        stop_event.wait(timeout=duration_sec)
        if not stop_event.is_set():
            logger.info("SessionManager: timer expired (%ds) — auto-stopping", duration_sec)
            stop_event.set()
        self._finalize_session()

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _finalize_session(self) -> Optional[dict]:
        with self._lock:
            active = self._active
            if active is None:
                return None

        meta = active["metadata"]
        meta["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
        meta["status"] = "done"
        self._save_json(active["session_dir"] / "metadata.json", meta)

        with self._lock:
            self._active  = None
            self._threads = []

        logger.info(
            "Session %s finalized — %d frames, %d sensor rows",
            meta["session_id"], meta["frame_count"], meta["sensor_rows"],
        )
        return meta

    def _assemble_mp4(self, session_dir: Path, frames_dir: Path, frame_count: int):
        """Assemble JPEG frames into an H.264 MP4 using cv2.VideoWriter."""
        if frame_count == 0:
            return
        try:
            import cv2

            fps    = getattr(config, "CAMERA_STREAM_WIDTH", None)  # use lores fps
            fps    = getattr(config, "CAMERA_FRAMERATE", 30)
            first  = cv2.imread(str(frames_dir / "000000.jpg"))
            if first is None:
                logger.warning("MP4 assembly: first frame unreadable — skipping")
                return
            h, w   = first.shape[:2]
            out_path = str(session_dir / "video.mp4")
            fourcc   = cv2.VideoWriter_fourcc(*"mp4v")
            writer   = cv2.VideoWriter(out_path, fourcc, fps, (w, h))

            for i in range(frame_count):
                p = frames_dir / f"{i:06d}.jpg"
                if p.exists():
                    img = cv2.imread(str(p))
                    if img is not None:
                        writer.write(img)
            writer.release()
            logger.info("MP4 assembled: %s (%d frames @ %d fps)", out_path, frame_count, fps)

        except ImportError:
            logger.warning("MP4 assembly skipped — cv2 not available")
        except Exception as exc:
            logger.error("MP4 assembly failed: %s", exc)

    def _next_session_id(self) -> str:
        """Return next available session ID like S001, S002 …"""
        existing = [
            d.name for d in self._sessions_dir.iterdir()
            if d.is_dir() and d.name.startswith("S") and d.name[1:4].isdigit()
        ]
        nums  = [int(n[1:4]) for n in existing if len(n) >= 4]
        next_n = max(nums, default=0) + 1
        return f"S{next_n:03d}"

    @staticmethod
    def _save_json(path: Path, data: dict):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
