"""
sensors/buzzer.py  –  Grove Buzzer v1.3 driver + alert logic.

Hardware: Active buzzer (HIGH = ON) connected to D5 port on Grove Base HAT.
D5 port = GPIO 5 on Raspberry Pi.
Uses lgpio (Pi 5 compatible — replaces RPi.GPIO).

Alert conditions and beep patterns:
  - SpO2 < ALERT_SPO2_LOW         → 3 short beeps        (most urgent)
  - HR > ALERT_HR_HIGH            → 2 fast beeps          (tachycardia)
  - HR < ALERT_HR_LOW             → 2 slow beeps          (bradycardia)
  - GSR > ALERT_GSR_HIGH_US       → 1 long beep           (stress)
  - sensor_error in data          → 1 long + 1 short beep (hardware fault)

Each condition has its own independent cooldown — a GSR alert will not
suppress a simultaneous SpO2 alert.
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
        # Per-condition cooldown: each key has its own last-fired timestamp.
        # Prevents one condition from suppressing another during cooldown.
        self._last_alert_time: dict[str, float] = {}
        self._lock = threading.Lock()   # prevent concurrent beep calls
        self._active = False            # is buzzer currently ON?

    # ── Setup / teardown ──────────────────────────────────────────────────

    def setup(self):
        """Initialise lgpio and configure the buzzer pin as output.

        IMPORTANT — Raspberry Pi 5 uses gpiochip4 (not gpiochip0).
        config.BUZZER_GPIO_CHIP must be set to 4 for Pi 5.
        """
        if not self._enabled:
            logger.info("Buzzer disabled in config — skipping setup.")
            return
        try:
            import lgpio
            chip = getattr(config, "BUZZER_GPIO_CHIP", 4)
            self._gpio_handle = lgpio.gpiochip_open(chip)
            # Drive LOW immediately — prevents floating HIGH buzz on startup
            lgpio.gpio_claim_output(self._gpio_handle, self._pin, 0)
            lgpio.gpio_write(self._gpio_handle, self._pin, 0)   # explicit LOW
            logger.info(
                "Buzzer ready on GPIO %d via gpiochip%d (D5, Grove Base HAT)",
                self._pin, chip,
            )
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
        Each condition fires independently — one alert will not suppress
        another during its cooldown window.

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

        # ── SpO2 (highest priority — check first) ─────────────────────────
        spo2 = data.get("spo2_percent")
        spo2_valid = data.get("spo2_valid", False)
        if spo2 is not None and spo2_valid:
            if config.ALERT_SPO2_LOW and spo2 < config.ALERT_SPO2_LOW:
                reasons.append(f"SPO2_LOW:{spo2:.1f}%<{config.ALERT_SPO2_LOW}")
                self._maybe_beep(
                    "SPO2",
                    durations=[config.BUZZER_SHORT_BEEP_S] * 3,
                    gap=0.1,
                )

        # ── Heart Rate ────────────────────────────────────────────────────
        hr = data.get("heart_rate_bpm")
        hr_valid = data.get("hr_valid", False)
        if hr is not None and hr_valid:
            if config.ALERT_HR_HIGH and hr > config.ALERT_HR_HIGH:
                reasons.append(f"HR_HIGH:{hr:.0f}bpm>{config.ALERT_HR_HIGH}")
                self._maybe_beep(
                    "HR_HIGH",
                    durations=[config.BUZZER_SHORT_BEEP_S] * 2,
                    gap=0.08,   # fast gap — tachycardia urgency
                )
            if config.ALERT_HR_LOW and hr < config.ALERT_HR_LOW:
                reasons.append(f"HR_LOW:{hr:.0f}bpm<{config.ALERT_HR_LOW}")
                self._maybe_beep(
                    "HR_LOW",
                    durations=[config.BUZZER_SHORT_BEEP_S] * 2,
                    gap=0.40,   # slow gap — bradycardia rhythm
                )

        # ── GSR ───────────────────────────────────────────────────────────
        gsr = data.get("gsr_conductance_us")
        if gsr is not None:
            if config.ALERT_GSR_HIGH_US and gsr > config.ALERT_GSR_HIGH_US:
                reasons.append(f"GSR_HIGH:{gsr:.2f}uS>{config.ALERT_GSR_HIGH_US}")
                self._maybe_beep(
                    "GSR",
                    durations=[config.BUZZER_LONG_BEEP_S],
                    gap=0,
                )

        # ── Sensor disconnect / hardware error ────────────────────────────
        sensor_error = data.get("sensor_error")
        if sensor_error:
            reasons.append(f"SENSOR_ERROR:{sensor_error}")
            # 1 long + 1 short: clearly distinguishable from other patterns
            self._maybe_beep(
                "SENSOR_ERROR",
                durations=[config.BUZZER_LONG_BEEP_S, config.BUZZER_SHORT_BEEP_S],
                gap=0.15,
            )

        alert_active = len(reasons) > 0
        return alert_active, reasons

    # ── Beep logic ────────────────────────────────────────────────────────

    def _maybe_beep(self, key: str, durations: list[float], gap: float):
        """
        Fire beep sequence only if this condition's cooldown has expired.
        Each condition key has an independent timer — SPO2 cooldown does
        not block HR_HIGH from firing at the same time.

        Parameters
        ----------
        key       : condition name — one of SPO2 / HR_HIGH / HR_LOW / GSR / SENSOR_ERROR
        durations : list of on-times (seconds) for each beep in the sequence
        gap       : silence between beeps (seconds)
        """
        now = time.monotonic()
        with self._lock:
            if (now - self._last_alert_time.get(key, 0.0)) < config.BUZZER_COOLDOWN_S:
                return  # this condition is still in cooldown
            self._last_alert_time[key] = now

        logger.warning("ALERT [%s]: %d beep(s)", key, len(durations))
        self._beep_sequence(durations, gap)

    def _beep_sequence(self, durations: list[float], gap: float):
        """Sound the buzzer with a variable-length sequence in a background thread."""
        def _play():
            for i, on_time in enumerate(durations):
                self._set_pin(True)
                time.sleep(on_time)
                self._set_pin(False)
                if gap > 0 and i < len(durations) - 1:
                    time.sleep(gap)

        threading.Thread(target=_play, daemon=True).start()

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
        """Two short beeps — call after setup() to verify buzzer works."""
        logger.info("Buzzer test beep...")
        self._beep_sequence([0.05, 0.05], gap=0.05)
