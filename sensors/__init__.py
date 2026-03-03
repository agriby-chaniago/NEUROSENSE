"""
sensors/__init__.py
Abstract base class for all NEUROSENSE sensors.

To add a new sensor:
  1. Create sensors/my_sensor_reader.py
  2. Subclass BaseSensor
  3. Implement read(), and optionally calibrate() / close()
  4. Register in config.ACTIVE_SENSORS and sensor_manager.py
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseSensor(ABC):
    """
    Minimal contract every sensor driver must fulfil.
    sensor_manager.py treats all sensors identically through this interface.
    """

    # Human-readable name used in logs and dashboard labels
    name: str = "unknown_sensor"

    @abstractmethod
    def read(self) -> dict:
        """
        Read one measurement from the sensor.

        Returns
        -------
        dict
            Keys must be a subset of config.CSV_FIELDNAMES.
            Include only the fields this sensor populates.
            Example: {"temperature_celsius": 25.3, "humidity_percent": 60.1}
        """

    def calibrate(self) -> None:
        """
        Optional one-time calibration step called before the read loop starts.
        Override in subclasses that need it (e.g. GSR baseline).
        """

    def close(self) -> None:
        """
        Optional cleanup called when the sensor thread stops.
        Override to release hardware resources (I2C handles, file descriptors).
        """

    def health(self) -> dict:
        """
        Return a status snapshot for the /health endpoint.
        Override for richer status; default assumes sensor is alive if last
        read succeeded (sensor_manager updates _last_error).
        """
        return {
            "sensor": self.name,
            "status": "ok" if not getattr(self, "_last_error", None) else "error",
            "error": getattr(self, "_last_error", None),
        }
