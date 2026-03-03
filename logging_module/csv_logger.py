"""
logging_module/csv_logger.py  –  Thread-safe CSV logger using a queue + single writer thread.

Design: all sensor threads enqueue rows; one dedicated writer thread
dequeues and writes. No lock contention on the file handle.
File is rotated daily: neurosense_YYYY-MM-DD.csv
"""

import csv
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Sentinel value to stop the writer thread cleanly
_STOP_SENTINEL = None


class CSVLogger:
    """
    Queue-based, thread-safe CSV logger.

    Usage
    -----
        csv_logger = CSVLogger()
        csv_logger.start()
        csv_logger.log({"timestamp_utc": ..., "temperature_celsius": 25.3, ...})
        csv_logger.stop()
    """

    def __init__(self, data_dir: str = config.DATA_DIR, fieldnames: list = config.CSV_FIELDNAMES):
        self._data_dir  = Path(data_dir)
        self._fieldnames = fieldnames
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._thread: threading.Thread | None = None
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        """Start the background writer thread."""
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="csv-writer",
            daemon=True,
        )
        self._thread.start()
        logger.info("CSVLogger started. Data directory: %s", self._data_dir)

    def stop(self):
        """Flush remaining rows and stop the writer thread."""
        logger.info("CSVLogger flushing queue (%d items)...", self._queue.qsize())
        self._queue.put(_STOP_SENTINEL)
        if self._thread:
            self._thread.join(timeout=10.0)
        logger.info("CSVLogger stopped.")

    def log(self, row: dict):
        """
        Enqueue a row for writing. Non-blocking — drops row if queue is full
        (prevents sensor threads from blocking on a slow disk).
        """
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            logger.warning("CSVLogger queue full — row dropped.")

    # ── Writer thread ──────────────────────────────────────────────────

    def _writer_loop(self):
        """Continuously dequeue rows and write them to the current day's CSV."""
        current_file: str | None = None
        file_handle = None
        writer = None

        try:
            while True:
                row = self._queue.get()

                if row is _STOP_SENTINEL:
                    logger.debug("CSVLogger writer received stop sentinel.")
                    break

                # Daily rotation — open a new file if date changed
                today_file = self._get_filepath()
                if today_file != current_file:
                    if file_handle:
                        file_handle.flush()
                        file_handle.close()
                    file_handle, writer = self._open_file(today_file)
                    current_file = today_file

                writer.writerow(row)
                file_handle.flush()   # ensure data is on disk even if Pi loses power
                self._queue.task_done()

        except Exception as exc:
            logger.error("CSVLogger writer thread crashed: %s", exc)
        finally:
            if file_handle:
                try:
                    file_handle.flush()
                    file_handle.close()
                except Exception:
                    pass

    def _get_filepath(self) -> str:
        """Return today's CSV file path."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return str(self._data_dir / f"neurosense_{date_str}.csv")

    def _open_file(self, filepath: str):
        """
        Open (or append to) a CSV file, writing header if it is new.
        Returns (file_handle, csv.DictWriter).
        """
        file_exists = Path(filepath).exists() and Path(filepath).stat().st_size > 0
        fh = open(filepath, "a", newline="", buffering=1, encoding="utf-8")
        writer = csv.DictWriter(
            fh,
            fieldnames=self._fieldnames,
            extrasaction="ignore",   # silently ignore extra keys from sensors
        )
        if not file_exists:
            writer.writeheader()
            logger.info("Created new CSV file: %s", filepath)
        else:
            logger.info("Appending to existing CSV file: %s", filepath)
        return fh, writer
