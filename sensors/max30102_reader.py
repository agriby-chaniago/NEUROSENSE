"""
sensors/max30102_reader.py  –  High-level MAX30102 sensor reader.
Uses polling mode (no INT pin) — compatible with Raspberry Pi 5.

Sliding-window ring buffer: collects STEP_SIZE new samples per call,
runs the HR/SpO2 algorithm on the latest SAMPLE_BUFFER samples.
First result appears after SAMPLE_BUFFER samples (~12 s at 25 Hz FIFO);
subsequent results appear every STEP_SIZE samples (~1 s at 25 Hz).
"""

import collections
import logging
from . import BaseSensor
from .max30102 import MAX30102
from .hrcalc import calc_hr_and_spo2
import config

logger = logging.getLogger(__name__)

# Effective FIFO rate = 25 Hz (SR=100 Hz / SMP_AVE=4) → 25 samples = 1 s per step.
_STEP_SIZE = 25   # ~1 s at 25 Hz FIFO


class MAX30102Reader(BaseSensor):
    name = "max30102"

    def __init__(self):
        self._sensor: MAX30102 | None = None
        self._last_error: str | None = None
        # Sliding-window ring buffers
        self._ring_ir:  collections.deque = collections.deque(
            maxlen=config.MAX30102_SAMPLE_BUFFER
        )
        self._ring_red: collections.deque = collections.deque(
            maxlen=config.MAX30102_SAMPLE_BUFFER
        )
        # EMA smoothing — absorbs single-read jitter
        self._ema_hr:   float | None = None
        self._ema_spo2: float | None = None
        self._EMA_A = 0.35   # weight for newest reading (higher = faster response)
        # Consecutive-reject counter: if the algorithm rejects N reads in a row
        # (noisy signal, bad placement), expire the stale EMA so the display
        # clears to — and buzzer stops firing on an old cached value.
        self._reject_count: int = 0
        self._EMA_MAX_REJECTS = 3   # clear after 3 consecutive bad reads

    def calibrate(self) -> None:
        """Initialise the MAX30102 and verify it responds with the correct PART_ID."""
        try:
            self._sensor = MAX30102(
                i2c_bus=config.I2C_BUS,
                address=config.MAX30102_I2C_ADDRESS,
            )
            self._ema_hr   = None   # reset smoothing on recalibrate
            self._ema_spo2 = None
            self._reject_count = 0
            self._ring_ir.clear()
            self._ring_red.clear()
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
        Collect _STEP_SIZE new samples, append to ring buffer, then run
        HR/SpO2 algorithm on the latest SAMPLE_BUFFER samples.

        Returns
        -------
        dict with keys: heart_rate_bpm, spo2_percent, hr_valid, spo2_valid
        """
        if self._sensor is None:
            raise RuntimeError("MAX30102Reader not calibrated; call calibrate() first.")

        try:
            # Collect a small step batch instead of the full buffer each time
            red_step, ir_step = self._sensor.read_sequential(
                sample_count=_STEP_SIZE
            )
            self._ring_ir.extend(ir_step)
            self._ring_red.extend(red_step)

            import numpy as _np  # local import, lightweight

            # Not enough samples yet to calculate
            if len(self._ring_ir) < config.MAX30102_SAMPLE_BUFFER:
                logger.debug(
                    "MAX30102 ring buffer filling: %d/%d samples",
                    len(self._ring_ir), config.MAX30102_SAMPLE_BUFFER,
                )
                return {
                    "heart_rate_bpm": None, "spo2_percent": None,
                    "hr_valid": False,      "spo2_valid":  False,
                }

            ir_data  = list(self._ring_ir)
            red_data = list(self._ring_red)

            logger.info(
                "MAX30102 buffer: ir_mean=%.0f  red_mean=%.0f  samples=%d",
                float(_np.mean(ir_data)), float(_np.mean(red_data)), len(ir_data),
            )
            hr, hr_valid, spo2, spo2_valid = calc_hr_and_spo2(
                ir_data, red_data,
                sampling_freq=config.MAX30102_SAMPLING_RATE_HZ // 4,  # SMP_AVE=4
            )
            self._last_error = None

            # Finger removed → hard-reset EMA + ring buffer so display clears to —
            finger_removed = (hr == -999.0 and not hr_valid)
            if finger_removed:
                self._ema_hr   = None
                self._ema_spo2 = None
                self._reject_count = 0
                self._ring_ir.clear()    # flush stale low-IR samples
                self._ring_red.clear()
            else:
                # Update EMA only on valid reads
                if hr_valid:
                    self._ema_hr = hr if self._ema_hr is None else \
                        self._EMA_A * hr + (1 - self._EMA_A) * self._ema_hr
                    self._reject_count = 0   # good read — reset counter
                else:
                    # Invalid read (noisy/transitional) but finger still present.
                    # Keep EMA for a few reads; clear after too many in a row.
                    self._reject_count += 1
                    if self._reject_count >= self._EMA_MAX_REJECTS:
                        self._ema_hr   = None
                        self._ema_spo2 = None
                if spo2_valid:
                    self._ema_spo2 = spo2 if self._ema_spo2 is None else \
                        self._EMA_A * spo2 + (1 - self._EMA_A) * self._ema_spo2

            # Show last known EMA even when current read is invalid (e.g. motion)
            # so the dashboard doesn't flicker back to — on every noisy frame.
            out_hr   = round(self._ema_hr,   1) if self._ema_hr   is not None else None
            out_spo2 = round(self._ema_spo2, 1) if self._ema_spo2 is not None else None

            result = {
                "heart_rate_bpm": out_hr,
                "spo2_percent":   out_spo2,
                "hr_valid":       out_hr   is not None,
                "spo2_valid":     out_spo2 is not None,
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
