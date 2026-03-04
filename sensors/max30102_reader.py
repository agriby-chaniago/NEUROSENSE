"""
sensors/max30102_reader.py  –  High-level MAX30102 sensor reader.
Uses polling mode (no INT pin) — compatible with Raspberry Pi 5.
"""

import logging
from . import BaseSensor
from .max30102 import MAX30102
from .hrcalc import calc_hr_and_spo2
import config

logger = logging.getLogger(__name__)


class MAX30102Reader(BaseSensor):
    name = "max30102"

    def __init__(self):
        self._sensor: MAX30102 | None = None
        self._last_error: str | None = None

    def calibrate(self) -> None:
        """Initialise the MAX30102 and verify it responds with the correct PART_ID."""
        try:
            self._sensor = MAX30102(
                i2c_bus=config.I2C_BUS,
                address=config.MAX30102_I2C_ADDRESS,
            )
            part_id = self._sensor.get_part_id()
            if part_id != 0x15:
                logger.warning(
                    "MAX30102: unexpected PART_ID 0x%02X (expected 0x15). "
                    "Some clone modules swap Red/IR channels.",
                    part_id,
                )
            else:
                logger.info("MAX30102 detected OK (PART_ID=0x15)")
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("MAX30102 calibrate failed: %s", exc)
            raise

    def read(self) -> dict:
        """
        Collect a buffer of samples and calculate HR + SpO2.

        Returns
        -------
        dict with keys: heart_rate_bpm, spo2_percent, hr_valid, spo2_valid
        """
        if self._sensor is None:
            raise RuntimeError("MAX30102Reader not calibrated; call calibrate() first.")

        try:
            red_data, ir_data = self._sensor.read_sequential(
                sample_count=config.MAX30102_SAMPLE_BUFFER
            )
            import numpy as _np  # local import, lightweight
            logger.info(
                "MAX30102 buffer: ir_mean=%.0f  red_mean=%.0f  samples=%d",
                float(_np.mean(ir_data)), float(_np.mean(red_data)), len(ir_data),
            )
            hr, hr_valid, spo2, spo2_valid = calc_hr_and_spo2(
                ir_data, red_data,
                sampling_freq=config.MAX30102_SAMPLING_RATE_HZ // 4,  # SMP_AVE=4
            )
            self._last_error = None
            result = {
                "heart_rate_bpm": hr if hr_valid else None,
                "spo2_percent":   spo2 if spo2_valid else None,
                "hr_valid":       hr_valid,
                "spo2_valid":     spo2_valid,
            }
            logger.debug("MAX30102: %s", result)
            return result

        except Exception as exc:
            self._last_error = str(exc)
            logger.error("MAX30102 read error: %s", exc)
            return {
                "heart_rate_bpm": None,
                "spo2_percent":   None,
                "hr_valid":       False,
                "spo2_valid":     False,
            }

    def close(self) -> None:
        if self._sensor:
            self._sensor.close()
            self._sensor = None
        logger.info("MAX30102 closed.")
