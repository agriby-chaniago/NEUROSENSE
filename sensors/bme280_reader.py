"""
sensors/bme280_reader.py  –  BME280 temperature / humidity / pressure reader.
Uses RPi.bme280 + smbus2 (lightweight, no Adafruit Blinka needed).
"""

import logging
import smbus2
import bme280 as _bme280_lib
from . import BaseSensor
import config

logger = logging.getLogger(__name__)


class BME280Reader(BaseSensor):
    name = "bme280"

    def __init__(self):
        self._bus: smbus2.SMBus | None = None
        self._params = None
        self._last_error: str | None = None

    def calibrate(self) -> None:
        """Open I2C bus, load BME280 calibration parameters."""
        try:
            self._bus = smbus2.SMBus(config.I2C_BUS)
            self._params = _bme280_lib.load_calibration_params(
                self._bus, config.BME280_I2C_ADDRESS
            )
            # Quick test read
            sample = _bme280_lib.sample(self._bus, config.BME280_I2C_ADDRESS, self._params)
            logger.info(
                "BME280 detected OK — init reading: %.1f°C, %.1f%%RH, %.1fhPa",
                sample.temperature, sample.humidity, sample.pressure,
            )
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("BME280 calibrate failed: %s", exc)
            raise

    def read(self) -> dict:
        """
        Read one sample from BME280.

        Returns
        -------
        dict with keys: temperature_celsius, humidity_percent, pressure_hpa
        """
        if self._bus is None or self._params is None:
            raise RuntimeError("BME280Reader not calibrated; call calibrate() first.")

        try:
            sample = _bme280_lib.sample(
                self._bus, config.BME280_I2C_ADDRESS, self._params
            )
            self._last_error = None
            result = {
                "temperature_celsius": round(sample.temperature, 2),
                "humidity_percent":    round(sample.humidity,    2),
                "pressure_hpa":        round(sample.pressure,    2),
            }
            logger.debug("BME280: %s", result)
            return result

        except Exception as exc:
            self._last_error = str(exc)
            logger.error("BME280 read error: %s", exc)
            return {
                "temperature_celsius": None,
                "humidity_percent":    None,
                "pressure_hpa":        None,
            }

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None
        logger.info("BME280 closed.")
