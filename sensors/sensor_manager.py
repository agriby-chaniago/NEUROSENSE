"""
sensors/sensor_manager.py  –  Orchestrates all active sensors using threads.

Design:
  - One background thread per sensor (slow sensors don't block fast ones)
  - Shared latest_data dict protected by a threading.Lock
  - SensorManager is iterable over sensor names by design — adding a new
    sensor only requires registering it in this file and in config.py
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import config
from logging_module.csv_logger import CSVLogger
from sensors.buzzer import Buzzer

logger = logging.getLogger(__name__)


class SensorManager:
    """
    Starts one reader thread per active sensor and keeps a shared
    `latest_data` dict up-to-date for the dashboard and CSV logger.
    """

    def __init__(self, csv_logger: CSVLogger):
        self._csv_logger = csv_logger
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._buzzer = Buzzer()

        # Initialised with sentinel Nones — dashboard can detect "not yet read"
        self.latest_data: dict[str, Any] = {k: None for k in config.CSV_FIELDNAMES}
        self.latest_data["alert_active"]  = False
        self.latest_data["alert_reasons"] = ""

        # Build the sensor registry from ACTIVE_SENSORS config
        self._sensors: dict = {}
        self._register_sensors()

    # ── Registry ─────────────────────────────────────────────────────────

    def _register_sensors(self):
        """
        Register all sensors that are enabled in config.ACTIVE_SENSORS.
        To add a new sensor: import its reader here, add an entry.
        """
        if config.ACTIVE_SENSORS.get("bme280"):
            from sensors.bme280_reader import BME280Reader
            self._sensors["bme280"] = {
                "reader":   BME280Reader(),
                "interval": config.BME280_INTERVAL_S,
            }

        if config.ACTIVE_SENSORS.get("max30102"):
            from sensors.max30102_reader import MAX30102Reader
            self._sensors["max30102"] = {
                "reader":   MAX30102Reader(),
                "interval": config.MAX30102_INTERVAL_S,
            }

        if config.ACTIVE_SENSORS.get("gsr"):
            from sensors.gsr_reader import GSRReader
            self._sensors["gsr"] = {
                "reader":   GSRReader(),
                "interval": config.GSR_INTERVAL_S,
            }

        logger.info("Registered sensors: %s", list(self._sensors.keys()))

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Calibrate all sensors, setup buzzer, then start reader threads."""
        self._buzzer.setup()
        if config.BUZZER_STARTUP_BEEP:
            self._buzzer.test_beep()   # 2 beep pendek = konfirmasi buzzer OK
        logger.info("Calibrating all sensors...")
        calibrated = {}
        for name, entry in self._sensors.items():
            try:
                entry["reader"].calibrate()
                calibrated[name] = entry
            except Exception as exc:
                logger.error("Sensor '%s' calibration failed — will skip: %s", name, exc)

        if not calibrated:
            logger.warning(
                "No sensors calibrated successfully. "
                "Check that I2C is enabled (sudo raspi-config → Interface Options → I2C) "
                "and all devices are wired correctly, then run: i2cdetect -y 1"
            )

        for name, entry in calibrated.items():
            t = threading.Thread(
                target=self._sensor_loop,
                args=(name, entry["reader"], entry["interval"]),
                name=f"sensor-{name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
            logger.info("Started thread for sensor '%s'", name)

    def stop(self):
        """Signal all sensor threads to stop and close sensors."""
        logger.info("Stopping sensor manager...")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5.0)
        for name, entry in self._sensors.items():
            try:
                entry["reader"].close()
            except Exception as exc:
                logger.warning("Error closing sensor '%s': %s", name, exc)
        self._buzzer.close()
        logger.info("Sensor manager stopped.")

    # ── Thread loop ──────────────────────────────────────────────────────

    def _sensor_loop(self, name: str, reader, interval: float):
        """Main loop for a single sensor thread."""
        logger.debug("Sensor loop starting: %s (interval=%.1fs)", name, interval)
        while not self._stop_event.is_set():
            start = time.monotonic()
            try:
                data = reader.read()
                self._update(data)
            except Exception as exc:
                logger.error("Sensor '%s' unhandled read error: %s", name, exc)

            elapsed = time.monotonic() - start
            sleep_time = max(0.0, interval - elapsed)
            self._stop_event.wait(timeout=sleep_time)

        logger.debug("Sensor loop exiting: %s", name)

    def _update(self, new_data: dict):
        """
        Merge new_data into latest_data (thread-safe), evaluate alerts,
        trigger buzzer if needed, then push to CSV.
        """
        with self._lock:
            now_utc = datetime.now(timezone.utc).isoformat()
            self.latest_data["timestamp_utc"]  = now_utc
            self.latest_data["schema_version"] = config.DATA_SCHEMA_VERSION
            self.latest_data.update(new_data)
            snapshot = dict(self.latest_data)

        # Evaluate alerts outside lock (buzzer runs in its own thread)
        alert_active, alert_reasons = self._buzzer.check_and_alert(snapshot)
        with self._lock:
            self.latest_data["alert_active"]  = alert_active
            self.latest_data["alert_reasons"] = ", ".join(alert_reasons)
            snapshot["alert_active"]  = alert_active
            snapshot["alert_reasons"] = ", ".join(alert_reasons)

        self._csv_logger.log(snapshot)

    # ── Health ───────────────────────────────────────────────────────────

    def health(self) -> list[dict]:
        """Return health dicts for all registered sensors."""
        return [entry["reader"].health() for entry in self._sensors.values()]

    def get_latest(self) -> dict:
        """Thread-safe snapshot of the latest aggregated sensor reading."""
        with self._lock:
            return dict(self.latest_data)
