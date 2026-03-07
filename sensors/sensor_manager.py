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
        # Track consecutive read failures per sensor to detect disconnects.
        # After _SENSOR_ERROR_THRESHOLD consecutive exceptions, a sensor_error
        # key is injected into latest_data → triggers buzzer disconnect alert.
        self._sensor_error_counts: dict[str, int] = {}
        self._SENSOR_ERROR_THRESHOLD = 5
        # Monotonic time of SensorManager construction — used to suppress
        # sensor_error alerts during the startup grace period (I2C bus needs
        # a few seconds to stabilise after boot before reads are reliable).
        self._start_mono: float = time.monotonic()
        self._STARTUP_GRACE_S = 20.0  # seconds to suppress sensor_error buzzer

        # Initialised with sentinel Nones — dashboard can detect "not yet read"
        self.latest_data: dict[str, Any] = {k: None for k in config.CSV_FIELDNAMES}
        self.latest_data["alert_active"]  = False
        self.latest_data["alert_reasons"] = ""
        self.latest_data["sensor_error"]  = None  # cleared by default; set on disconnect

        # Monotonic timestamp of the last successful sensor _update().
        # Used to detect sensor dropout: >5s without update = stale.
        self._last_update_mono: float = 0.0

        # Build the sensor registry from ACTIVE_SENSORS config
        self._sensors: dict = {}
        self._register_sensors()

    # ── Registry ─────────────────────────────────────────────────────────

    def _register_sensors(self):
        """
        Register all sensors that are enabled in config.ACTIVE_SENSORS.
        To add a new sensor: import its reader here, add an entry.
        Import errors are caught individually — a missing library for one
        sensor will not prevent the others from starting.
        """
        candidates = {
            "bmp280":   ("sensors.bmp280_reader",   "BMP280Reader",   config.BMP280_INTERVAL_S),
            "max30102": ("sensors.max30102_reader",  "MAX30102Reader", config.MAX30102_INTERVAL_S),
            "gsr":      ("sensors.gsr_reader",       "GSRReader",      config.GSR_INTERVAL_S),
            "ads1115":  ("sensors.ads1115_reader",   "ADS1115Reader",  config.ADS1115_INTERVAL_S),
        }

        for name, (module_path, class_name, interval) in candidates.items():
            if not config.ACTIVE_SENSORS.get(name):
                continue
            try:
                import importlib
                module = importlib.import_module(module_path)
                reader_class = getattr(module, class_name)
                self._sensors[name] = {
                    "reader":   reader_class(),
                    "interval": interval,
                }
            except ImportError as exc:
                logger.error(
                    "Sensor '%s' skipped — missing library: %s. "
                    "Run: pip install -r requirements.txt",
                    name, exc,
                )
            except Exception as exc:
                logger.error("Sensor '%s' failed to load: %s", name, exc)

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
                # Reset error counter on successful read
                if self._sensor_error_counts.get(name, 0) > 0:
                    logger.info("Sensor '%s' recovered after %d consecutive errors",
                                name, self._sensor_error_counts[name])
                    self._sensor_error_counts[name] = 0
                    self._update({"sensor_error": None})  # clear error flag
                self._update(data)
            except Exception as exc:
                logger.error("Sensor '%s' unhandled read error: %s", name, exc)
                count = self._sensor_error_counts.get(name, 0) + 1
                self._sensor_error_counts[name] = count
                # Only act when threshold is crossed for the first time (==),
                # NOT on every subsequent failure (>= would re-inject every loop
                # iteration and cause non-stop buzzing with interval=0 sensors).
                if count == self._SENSOR_ERROR_THRESHOLD:
                    uptime = time.monotonic() - self._start_mono
                    if uptime < self._STARTUP_GRACE_S:
                        # I2C bus not yet stable — log but don't alert
                        logger.warning(
                            "Sensor '%s' failed %d times but still in startup "
                            "grace period (%.0fs / %.0fs) — alert suppressed",
                            name, count, uptime, self._STARTUP_GRACE_S,
                        )
                    else:
                        logger.error(
                            "Sensor '%s' has failed %d consecutive times — "
                            "suspected disconnect", name, count,
                        )
                        self._update({"sensor_error": f"{name}_disconnected"})

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
            self._last_update_mono = time.monotonic()
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
        """Thread-safe snapshot of the latest aggregated sensor reading.

        Includes 'sensor_stale': True when no sensor has updated in >5 s
        (e.g. all sensor threads frozen / hardware disconnected).
        """
        with self._lock:
            snapshot = dict(self.latest_data)
            elapsed = time.monotonic() - self._last_update_mono
            # _last_update_mono starts at 0.0 — don't flag stale for the first
            # 10 s while sensors are still calibrating.
            snapshot["sensor_stale"] = (
                self._last_update_mono > 0.0 and elapsed > 5.0
            )
            snapshot["sensor_last_update_s"] = round(elapsed, 1) if self._last_update_mono > 0.0 else None
        return snapshot

    def recalibrate_sensor(self, name: str) -> dict:
        """
        Trigger runtime recalibration for a specific sensor by name.
        Currently supported: 'gsr'

        Returns
        -------
        dict with recalibration result info
        """
        entry = self._sensors.get(name)
        if not entry:
            raise ValueError(f"Sensor '{name}' not registered or not active.")
        reader = entry["reader"]
        if not hasattr(reader, "recalibrate_baseline"):
            raise NotImplementedError(f"Sensor '{name}' does not support runtime recalibration.")
        return reader.recalibrate_baseline()
