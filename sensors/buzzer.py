"""
sensors/buzzer.py  –  Grove Buzzer v1.3 driver + alert logic.

Hardware: Active buzzer (HIGH = ON) connected to D5 port on Grove Base HAT.
D5 port = GPIO 5 on Raspberry Pi.
Uses lgpio (Pi 5 compatible — replaces RPi.GPIO).

Alert conditions checked:
  - Heart Rate > ALERT_HR_HIGH or < ALERT_HR_LOW
  - SpO2 < ALERT_SPO2_LOW
  - GSR conductance > ALERT_GSR_HIGH_US
  - Sensor disconnect (sensor_error)
"""

import logging
import time
import threading
from typing import Any

import config

logger = logging.getLogger(__name__)


class Buzzer:
    """
    Controls the Grove Buzzer v1.3 and evaluates alert conditions.

    Usage
    -----
        buzzer = Buzzer()
        buzzer.setup()
        buzzer.check_and_alert(latest_data)   # call after each sensor update
        buzzer.close()
    """

    def __init__(self):
        self._gpio_handle = None
        self._pin = config.BUZZER_GPIO_PIN
        self._enabled = config.BUZZER_ENABLED
        self._last_alert_time: float = 0.0
        self._lock = threading.Lock()   # prevent concurrent beep calls
        self._active = False            # is buzzer currently ON?

    # ── Setup / teardown ──────────────────────────────────────────────────

    def setup(self):
        """Initialise lgpio and configure the buzzer pin as output."""
        if not self._enabled:
            logger.info("Buzzer disabled in config — skipping setup.")
            return
        try:
            import lgpio
            self._gpio_handle = lgpio.gpiochip_open(0)
            # Immediately drive LOW — prevents floating HIGH buzz if Pi booted
            # without gpio=5=op,dl in /boot/firmware/config.txt
            lgpio.gpio_claim_output(self._gpio_handle, self._pin, 0)
            lgpio.gpio_write(self._gpio_handle, self._pin, 0)   # explicit LOW
            logger.info("Buzzer ready on GPIO %d (D5 port, Grove Base HAT)", self._pin)
        except Exception as exc:
            logger.error("Buzzer setup failed: %s", exc)
            self._gpio_handle = None

    def close(self):
        """Turn off buzzer and release GPIO."""
        self._set_pin(False)
        if self._gpio_handle is not None:
            try:
                import lgpio
                lgpio.gpiochip_close(self._gpio_handle)
            except Exception:
                pass
            self._gpio_handle = None
        logger.info("Buzzer closed.")

    # ── Alert evaluation ──────────────────────────────────────────────────

    def check_and_alert(self, data: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Evaluate latest sensor data against alert thresholds.
        Triggers the buzzer if any threshold is exceeded and cooldown has passed.

        Parameters
        ----------
        data : dict
            Latest reading from SensorManager.get_latest()

        Returns
        -------
        (alert_active, alert_reasons)
            alert_active  : bool — True if any threshold was exceeded
            alert_reasons : list[str] — human-readable descriptions
        """
        reasons = []

        # ── Heart Rate ────────────────────────────────────────────────────
        hr = data.get("heart_rate_bpm")
        hr_valid = data.get("hr_valid", False)
        if hr is not None and hr_valid:
            if config.ALERT_HR_HIGH and hr > config.ALERT_HR_HIGH:
                reasons.append(f"HR_HIGH:{hr:.0f}bpm>{config.ALERT_HR_HIGH}")
            if config.ALERT_HR_LOW and hr < config.ALERT_HR_LOW:
                reasons.append(f"HR_LOW:{hr:.0f}bpm<{config.ALERT_HR_LOW}")

        # ── SpO2 ──────────────────────────────────────────────────────────
        spo2 = data.get("spo2_percent")
        spo2_valid = data.get("spo2_valid", False)
        if spo2 is not None and spo2_valid:
            if config.ALERT_SPO2_LOW and spo2 < config.ALERT_SPO2_LOW:
                reasons.append(f"SPO2_LOW:{spo2:.1f}%<{config.ALERT_SPO2_LOW}")

        # ── GSR ───────────────────────────────────────────────────────────
        gsr = data.get("gsr_conductance_us")
        if gsr is not None:
            if config.ALERT_GSR_HIGH_US and gsr > config.ALERT_GSR_HIGH_US:
                reasons.append(f"GSR_HIGH:{gsr:.2f}uS>{config.ALERT_GSR_HIGH_US}")

        # ── Trigger buzzer ────────────────────────────────────────────────
        alert_active = len(reasons) > 0
        if alert_active:
            self._trigger(reasons)

        return alert_active, reasons

    # ── Buzzer patterns ───────────────────────────────────────────────────

    def _trigger(self, reasons: list[str]):
        """
        Sound the buzzer if cooldown has passed.
        Pattern depends on severity:
          - SpO2 low          → 3 short beeps (urgent)
          - HR out of range   → 2 short beeps
          - GSR high          → 1 long beep
        """
        now = time.monotonic()
        with self._lock:
            if (now - self._last_alert_time) < config.BUZZER_COOLDOWN_S:
                return   # still in cooldown
            self._last_alert_time = now

        logger.warning("ALERT triggered: %s", ", ".join(reasons))

        # Choose pattern based on most severe condition
        if any("SPO2_LOW" in r for r in reasons):
            self._beep_pattern(count=3, on_time=config.BUZZER_SHORT_BEEP_S, gap=0.1)
        elif any("HR_" in r for r in reasons):
            self._beep_pattern(count=2, on_time=config.BUZZER_SHORT_BEEP_S, gap=0.1)
        else:
            self._beep_pattern(count=1, on_time=config.BUZZER_LONG_BEEP_S, gap=0)

    def _beep_pattern(self, count: int, on_time: float, gap: float):
        """Sound the buzzer `count` times in a background thread."""
        def _beep():
            for i in range(count):
                self._set_pin(True)
                time.sleep(on_time)
                self._set_pin(False)
                if gap > 0 and i < count - 1:
                    time.sleep(gap)

        t = threading.Thread(target=_beep, daemon=True)
        t.start()

    def _set_pin(self, state: bool):
        """Set buzzer GPIO pin HIGH (on) or LOW (off)."""
        if self._gpio_handle is None:
            return
        try:
            import lgpio
            lgpio.gpio_write(self._gpio_handle, self._pin, 1 if state else 0)
            self._active = state
        except Exception as exc:
            logger.error("Buzzer GPIO write failed: %s", exc)

    # ── Manual test ───────────────────────────────────────────────────────

    def test_beep(self):
        """Single short beep — call after setup() to verify buzzer works."""
        logger.info("Buzzer test beep...")
        self._beep_pattern(count=2, on_time=0.05, gap=0.05)
