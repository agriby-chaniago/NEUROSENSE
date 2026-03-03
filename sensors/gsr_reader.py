"""
sensors/gsr_reader.py  –  Grove GSR Sensor reader via Grove Base HAT ADC.
The HAT's onboard STM32/MM32 exposes a 12-bit ADC over I2C (address 0x04 / 0x08).
Raw ADC → skin resistance (Ω) → skin conductance (µS / EDA signal).
"""

import logging
import time
from . import BaseSensor
import config

logger = logging.getLogger(__name__)

# Number of samples averaged during calibration baseline
_CALIBRATION_SAMPLES = 20
_CALIBRATION_DELAY_S = 0.05


class GSRReader(BaseSensor):
    name = "gsr"

    def __init__(self):
        self._adc = None
        self._calibration_value: int | None = None  # 10-bit equivalent
        self._last_error: str | None = None

    def calibrate(self) -> None:
        """
        Initialise Grove Base HAT ADC and take a baseline reading.
        Ask user to NOT touch the sensor during calibration.
        """
        try:
            from grove.adc import ADC
            self._adc = ADC(address=config.GROVE_HAT_ADC_ADDRESS)
            logger.info(
                "GSR: Grove Base HAT ADC connected (address=0x%02X, channel=%d). "
                "Calibrating baseline — do NOT touch the sensor...",
                config.GROVE_HAT_ADC_ADDRESS,
                config.GSR_GROVE_CHANNEL,
            )
            self._take_baseline()
            self._last_error = None

        except Exception as exc:
            self._last_error = str(exc)
            logger.error("GSR calibrate failed: %s", exc)
            raise

    def recalibrate_baseline(self) -> dict:
        """
        Retake the baseline WITHOUT reinitialising the ADC connection.
        Call this at runtime (e.g. from the dashboard Recalibrate button).
        The sensor must NOT be worn/touched during the ~1 second this takes.

        Returns
        -------
        dict with keys: baseline_raw, baseline_10bit, max_conductance_us
        """
        if self._adc is None:
            raise RuntimeError("GSRReader not initialised; call calibrate() first.")
        logger.info("GSR: recalibrating baseline — do NOT touch sensor...")
        self._take_baseline()
        max_us = round(1_000_000 * self._calibration_value / (1024 * 10_000), 2)
        result = {
            "baseline_10bit":      self._calibration_value,
            "max_conductance_us":  max_us,
        }
        logger.info("GSR recalibration done: %s", result)
        return result

    def _take_baseline(self) -> None:
        """Collect baseline samples and store the average as calibration value."""
        samples = []
        for _ in range(_CALIBRATION_SAMPLES):
            raw = self._adc.read(config.GSR_GROVE_CHANNEL)
            samples.append(raw)
            time.sleep(_CALIBRATION_DELAY_S)

        avg_raw = sum(samples) // len(samples)
        self._calibration_value = self._to_10bit(avg_raw)
        max_us = round(1_000_000 * self._calibration_value / (1024 * 10_000), 2)
        logger.info(
            "GSR baseline: raw=%d  10-bit=%d  max_conductance=%.2fµS",
            avg_raw, self._calibration_value, max_us,
        )

    def read(self) -> dict:
        """
        Read one GSR sample and compute resistance + conductance.

        Returns
        -------
        dict with keys: gsr_raw_adc, gsr_resistance_ohm, gsr_conductance_us
        """
        if self._adc is None:
            raise RuntimeError("GSRReader not calibrated; call calibrate() first.")

        try:
            raw_12bit = self._adc.read(config.GSR_GROVE_CHANNEL)
            reading_10bit = self._to_10bit(raw_12bit)

            resistance_ohm = self._calc_resistance(
                reading_10bit, self._calibration_value
            )
            conductance_us = (
                round(1_000_000.0 / resistance_ohm, 4)
                if resistance_ohm and resistance_ohm > 0
                else None
            )

            self._last_error = None
            result = {
                "gsr_raw_adc":         raw_12bit,
                "gsr_resistance_ohm":  round(resistance_ohm, 2) if resistance_ohm else None,
                "gsr_conductance_us":  conductance_us,
            }
            logger.debug("GSR: %s", result)
            return result

        except Exception as exc:
            self._last_error = str(exc)
            logger.error("GSR read error: %s", exc)
            return {
                "gsr_raw_adc":        None,
                "gsr_resistance_ohm": None,
                "gsr_conductance_us": None,
            }

    # ── Conversion helpers ────────────────────────────────────────────────

    @staticmethod
    def _to_10bit(value_12bit: int) -> int:
        """Scale 12-bit ADC value (0–4095) to 10-bit (0–1023) for Seeed formula."""
        return int(value_12bit * 1023 / 4095)

    @staticmethod
    def _calc_resistance(reading_10bit: int, calibration_10bit: int) -> float | None:
        """
        Convert 10-bit ADC reading to skin resistance in Ohms.

        Formula from Seeed Studio Grove GSR wiki (LM324-based circuit):
            R_human = ((1024 + 2 * reading) * 10000) / (calibration - reading)

        Returns None if denominator is zero (sensor not wearing / open circuit).
        """
        denominator = calibration_10bit - reading_10bit
        if denominator <= 0:
            return None
        resistance = ((1024 + 2 * reading_10bit) * 10000) / denominator
        return resistance

    def close(self) -> None:
        self._adc = None
        logger.info("GSR closed.")
