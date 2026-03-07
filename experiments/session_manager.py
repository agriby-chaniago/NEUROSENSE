"""
experiments/session_manager.py  –  Manages experiment recording sessions.

A session = one respondent × one condition × a fixed recording duration.

Directory layout per session
-----------------------------
    data/sessions/{session_id}/
        metadata.json          – session info (respondent, timestamps, counts)
        sensor_raw.csv         – sensor readings ~10 Hz, timestamp_ms = Unix epoch ms
        frame_timestamps.csv   – frame_idx, timestamp_ms (Unix epoch ms) per frame
        frames/                – JPEG frames  000000.jpg, 000001.jpg …
        video.mp4              – assembled from frames after session stops

Synchronisation
---------------
  Both sensor_raw.csv and frame_timestamps.csv use the same time axis:
  timestamp_ms = int(time.time() * 1000)  (Unix epoch milliseconds, UTC)
  Post-hoc alignment: pandas.merge_asof(sensor_df, frame_df, on="timestamp_ms")

Recording threads
-----------------
  sensor loop   : polls SensorManager.get_latest() every 100 ms → sensor_raw.csv
  video loop    : calls CameraReader.get_new_frame() → frames/ + frame_timestamps.csv
  timer loop    : sets stop_event after duration_sec → triggers finalize
"""

import csv
import json
import logging
import os
import queue
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
    "hr_valid",          # bool — False means value is stale/invalid; exclude from analysis
    "spo2_percent",
    "spo2_valid",        # bool — False means value is stale/invalid; exclude from analysis
    "temperature_celsius",
    "gsr_raw_adc",
    "gsr_conductance_us",
]




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
        self._last_meta: Optional[dict] = None  # metadata of last completed session
        self._threads: list[threading.Thread] = []

        sessions_dir = getattr(
            config, "EXPERIMENT_SESSIONS_DIR",
            os.path.join(config.DATA_DIR, "sessions"),
        )
        self._sessions_dir = Path(sessions_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        # Heal any sessions that were left in "recording" state (e.g. Pi crash)
        self._heal_interrupted_sessions()

    # ─── Session lifecycle ────────────────────────────────────────────────

    def start_session(
        self,
        respondent_id: str,
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
        """
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    "Another session is already active. "
                    f"Stop session {self._active['metadata']['session_id']} first."
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
            "Session %s started — respondent=%s  duration=%ds",
            session_id, respondent_id, duration_sec,
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

        # After joining all threads, _timer_loop may have already called
        # _finalize_session() which sets self._active = None.
        # Try to finalize; if already done, use _last_meta as fallback.
        result = self._finalize_session()
        if result is None:
            with self._lock:
                result = self._last_meta
        return result

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
            if not d.is_dir():          # skip stray files
                continue
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

    def delete_session(self, session_id: str) -> bool:
        """
        Permanently delete a session directory and all its data.

        Returns True if deleted, False if not found.
        Raises RuntimeError if the session is currently active/recording.
        """
        with self._lock:
            if self._active and self._active["metadata"]["session_id"] == session_id:
                raise RuntimeError(
                    f"Cannot delete session {session_id} — it is currently recording."
                )

        session_dir = self._sessions_dir / session_id
        if not session_dir.is_dir():
            return False

        import shutil as _shutil
        _shutil.rmtree(session_dir)
        logger.info("Session %s deleted.", session_id)
        return True

    # ─── Recording threads ────────────────────────────────────────────────

    def _sensor_loop(
        self,
        session_dir: Path,
        stop_event: threading.Event,
        metadata: dict,
    ):
        """Record sensor readings to sensor_raw.csv at ~10 Hz.

        timestamp_ms is Unix epoch milliseconds (int(time.time()*1000)) so it
        shares the same time axis as frame_timestamps.csv for alignment.
        """
        if self._sensor_manager is None:
            logger.warning("SessionManager: no sensor_manager — sensor recording skipped")
            return

        csv_path  = session_dir / "sensor_raw.csv"
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
                    "timestamp_ms":        int(time.time() * 1000),  # Unix epoch ms
                    "heart_rate_bpm":      data.get("heart_rate_bpm"),
                    "hr_valid":            data.get("hr_valid"),
                    "spo2_percent":        data.get("spo2_percent"),
                    "spo2_valid":          data.get("spo2_valid"),
                    "temperature_celsius": data.get("temperature_celsius"),
                    "gsr_raw_adc":         data.get("gsr_raw_adc"),
                    "gsr_conductance_us":  data.get("gsr_conductance_us"),
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
        """Save JPEG frames + frame_timestamps.csv, then assemble MP4 after stop.

        frame_timestamps.csv columns: frame_idx, timestamp_ms
        timestamp_ms = Unix epoch milliseconds — same axis as sensor_raw.csv.
        Written and flushed incrementally so no timestamps are lost on crash.
        """
        if self._camera_reader is None:
            logger.warning("SessionManager: no camera_reader — video recording skipped")
            return

        frame_count = 0
        ts_path = session_dir / "frame_timestamps.csv"

        with open(ts_path, "w", newline="", encoding="utf-8") as ts_fh:
            ts_writer = csv.writer(ts_fh)
            ts_writer.writerow(["frame_idx", "timestamp_ms"])

            # ── Async disk-write thread ──────────────────────────────────────
            # Separating frame capture from disk-I/O prevents SD card latency
            # (can be 10-30ms per write) from causing frame drops in the
            # get_new_frame() loop.  Queue holds up to 4 seconds of frames.
            _write_q: queue.Queue = queue.Queue(
                maxsize=int(getattr(config, "CAMERA_FRAMERATE", 45) * 4)
            )

            def _disk_writer():
                while True:
                    item = _write_q.get()
                    if item is None:  # poison pill
                        break
                    idx, frame_bytes, ts_ms = item
                    try:
                        (frames_dir / f"{idx:06d}.jpg").write_bytes(frame_bytes)
                        ts_writer.writerow([idx, ts_ms])
                        ts_fh.flush()
                    except Exception as exc:
                        logger.error("Session disk-write error frame %d: %s", idx, exc)

            _disk_thread = threading.Thread(
                target=_disk_writer, name="session-disk", daemon=True
            )
            _disk_thread.start()

            while not stop_event.is_set():
                frame = self._camera_reader.get_new_frame(timeout=0.5)
                if frame is None:
                    continue
                ts_ms = int(time.time() * 1000)  # capture timestamp before any I/O
                try:
                    _write_q.put_nowait((frame_count, frame, ts_ms))
                    frame_count += 1
                except queue.Full:
                    logger.warning(
                        "Session %s: disk-write queue full — frame %d dropped "
                        "(disk too slow)",
                        metadata["session_id"], frame_count,
                    )
                    # do NOT increment frame_count — keeps file numbering contiguous

            # Drain the write queue before exiting
            _write_q.put(None)   # poison pill
            _disk_thread.join(timeout=120.0)

        with self._lock:
            if self._active:
                self._active["metadata"]["frame_count"] = frame_count

        logger.info("Session %s video loop done: %d frames", metadata["session_id"], frame_count)
        self._assemble_mp4(session_dir, frames_dir, frame_count)

    def _timer_loop(self, duration_sec: int, stop_event: threading.Event):
        """Signal stop after duration_sec, then finalize the session."""
        stop_event.wait(timeout=duration_sec)
        if not stop_event.is_set():
            logger.info("SessionManager: timer expired (%ds) — auto-stopping", duration_sec)
            stop_event.set()

        # Wait for sensor and video loops to finish writing their final row/frame
        # counts to metadata before we finalize and save metadata.json.
        # We must NOT join ourselves (current thread = t_timer).
        current = threading.current_thread()
        for t in list(self._threads):
            if t is not current:
                t.join(timeout=60.0)  # 60 s generous budget for MP4 assembly

        # Finalize here only for the auto-stop path.
        # If stop_session() was called first it already joined this thread after
        # setting the stop_event, so _finalize_session() will be a safe no-op
        # (returns None) and stop_session() uses _last_meta as the fallback.
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
            self._last_meta = meta   # persist so stop_session() can return it
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

            fps      = getattr(config, "CAMERA_FRAMERATE", 30)
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

    def _heal_interrupted_sessions(self):
        """
        Called once at startup.  Any session whose metadata.json still has
        status='recording' was interrupted (Pi crash / power loss).  Mark it
        as 'interrupted' so it never blocks a new session from starting and
        the UI clearly shows the data may be incomplete.
        """
        healed = 0
        for d in self._sessions_dir.iterdir():
            if not d.is_dir():
                continue
            meta_file = d / "metadata.json"
            if not meta_file.exists():
                continue
            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("status") == "recording":
                    meta["status"] = "interrupted"
                    meta["stopped_at_utc"] = datetime.now(timezone.utc).isoformat()
                    meta["interrupted_reason"] = "Process restart — session was not stopped cleanly"
                    self._save_json(meta_file, meta)
                    healed += 1
                    logger.warning(
                        "Session %s marked as interrupted (was still 'recording' at startup)",
                        meta.get("session_id", d.name),
                    )
            except Exception as exc:
                logger.warning("Could not heal session %s: %s", d.name, exc)
        if healed:
            logger.info("Healed %d interrupted session(s) at startup", healed)

    @staticmethod
    def _save_json(path: Path, data: dict):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
