"""
tests/test_sensors.py  –  Unit tests using mocked I2C hardware.
Run on any machine (no Raspberry Pi required):
    pytest tests/
"""

import sys
import types
from unittest.mock import MagicMock, patch, call
import pytest

# ── Mock hardware modules so tests run on any machine ────────────────────────
# smbus2
smbus2_mock = MagicMock()
sys.modules["smbus2"] = smbus2_mock

# RPi.bme280
bme280_mock = MagicMock()
sys.modules["bme280"] = bme280_mock

# grove.adc
grove_pkg  = types.ModuleType("grove")
grove_adc  = types.ModuleType("grove.adc")
grove_adc.ADC = MagicMock()
sys.modules["grove"]     = grove_pkg
sys.modules["grove.adc"] = grove_adc

# lgpio
sys.modules["lgpio"] = MagicMock()
# ─────────────────────────────────────────────────────────────────────────────
# Now it's safe to import project modules
import config
from sensors.hrcalc import calc_hr_and_spo2, _find_peaks
from sensors.gsr_reader import GSRReader
from sensors.buzzer import Buzzer


# ══ hrcalc tests ═════════════════════════════════════════════════════════════

class TestFindPeaks:
    def test_simple_peaks(self):
        signal = [0, 1, 0, 0, 1, 0, 0, 1, 0]
        peaks  = _find_peaks(signal, min_distance=2)
        assert 1 in peaks
        assert 4 in peaks
        assert 7 in peaks

    def test_min_distance_respected(self):
        # Two close peaks — only the first should be kept
        signal = [0, 1, 0, 1, 0, 0, 0, 1, 0]
        peaks  = _find_peaks(signal, min_distance=4)
        for i in range(len(peaks) - 1):
            assert peaks[i + 1] - peaks[i] >= 4

    def test_no_data_returns_empty(self):
        assert _find_peaks([], min_distance=5) == []

    def test_flat_signal_no_peaks(self):
        signal = [5] * 20
        assert _find_peaks(signal, min_distance=5) == []


class TestCalcHrAndSpo2:
    def _synthetic_hr(self, bpm: int = 72, n_samples: int = 300, fs: int = 100) -> list:
        """
        Generate a synthetic pulsatile IR signal at a given BPM.
        n_samples=300 ensures at least 3 full cycles even at low BPM (e.g. 60 BPM
        = period 100 samples → 3 peaks in 300 samples), satisfying the ≥2 peak
        requirement in the HR algorithm.
        """
        import math
        freq = bpm / 60.0
        dc   = 100_000
        return [
            int(dc + 5000 * math.sin(2 * math.pi * freq * i / fs))
            for i in range(n_samples)
        ]

    def test_valid_hr_range(self):
        ir  = self._synthetic_hr(bpm=75)
        red = self._synthetic_hr(bpm=75)
        hr, hr_valid, spo2, spo2_valid = calc_hr_and_spo2(ir, red, sampling_freq=100)
        assert hr_valid, f"Expected valid HR, got {hr}"
        assert 50 <= hr <= 100, f"HR {hr} BPM out of expected range"

    def test_no_finger_returns_invalid(self):
        """Very low IR signal → no finger detected."""
        ir  = [100] * 100   # below 5000 threshold
        red = [100] * 100
        hr, hr_valid, spo2, spo2_valid = calc_hr_and_spo2(ir, red)
        assert not hr_valid
        assert not spo2_valid

    def test_too_few_samples_returns_invalid(self):
        ir  = [50000] * 10
        red = [50000] * 10
        hr, hr_valid, _, _ = calc_hr_and_spo2(ir, red)
        assert not hr_valid


# ══ GSR conversion tests ══════════════════════════════════════════════════════

class TestGSRConversions:
    def test_to_10bit_min(self):
        assert GSRReader._to_10bit(0) == 0

    def test_to_10bit_max(self):
        result = GSRReader._to_10bit(4095)
        assert result == 1023

    def test_to_10bit_midpoint(self):
        result = GSRReader._to_10bit(2048)
        assert 510 <= result <= 512   # ≈ 511

    def test_resistance_formula_known_value(self):
        """
        Known value from Seeed wiki example:
        reading=512, calibration=800 → R ≈ (1024+1024)*10000/(800-512)
                                          = 20480000/288 ≈ 71111 Ω
        """
        r = GSRReader._calc_resistance(512, 800)
        assert r is not None
        assert abs(r - 71111) < 5   # allow rounding

    def test_resistance_returns_none_when_denominator_zero(self):
        """reading == calibration → division by zero → None."""
        r = GSRReader._calc_resistance(500, 500)
        assert r is None

    def test_resistance_returns_none_when_denominator_negative(self):
        """reading > calibration (sensor disconnected state) → None."""
        r = GSRReader._calc_resistance(600, 400)
        assert r is None


# ══ CSVLogger tests ═══════════════════════════════════════════════════════════

class TestCSVLogger:
    def test_logger_enqueues_and_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        from logging_module.csv_logger import CSVLogger
        import time

        logger = CSVLogger(data_dir=str(tmp_path))
        logger.start()
        logger.log({
            "timestamp_utc":      "2026-03-03T00:00:00+00:00",
            "schema_version":     "1.0.0",
            "heart_rate_bpm":     72,
            "temperature_celsius": 25.3,
        })
        time.sleep(0.2)   # give writer thread time to flush
        logger.stop()

        csv_files = list(tmp_path.glob("neurosense_*.csv"))
        assert len(csv_files) == 1, "Expected one CSV file to be created"

        content = csv_files[0].read_text(encoding="utf-8")
        assert "timestamp_utc" in content   # header written
        assert "72" in content              # data written


# ══ Buzzer alert threshold tests ══════════════════════════════════════════════

class TestBuzzerAlerts:
    """
    Test alert evaluation logic only — no GPIO hardware required.
    The buzzer's _trigger() is patched so no actual beeping occurs.
    """

    def _make_buzzer(self) -> Buzzer:
        """Return a Buzzer instance with GPIO disabled (no hardware)."""
        b = Buzzer()
        b._gpio_handle = None   # simulate: gpio not initialised
        return b

    def test_hr_high_triggers_alert(self):
        b = self._make_buzzer()
        data = {"heart_rate_bpm": 130, "hr_valid": True,
                "spo2_percent": 98, "spo2_valid": True,
                "gsr_conductance_us": 5.0}
        active, reasons = b.check_and_alert(data)
        assert active
        assert any("HR_HIGH" in r for r in reasons)

    def test_hr_low_triggers_alert(self):
        b = self._make_buzzer()
        data = {"heart_rate_bpm": 40, "hr_valid": True,
                "spo2_percent": 98, "spo2_valid": True,
                "gsr_conductance_us": 5.0}
        active, reasons = b.check_and_alert(data)
        assert active
        assert any("HR_LOW" in r for r in reasons)

    def test_spo2_low_triggers_alert(self):
        b = self._make_buzzer()
        data = {"heart_rate_bpm": 75, "hr_valid": True,
                "spo2_percent": 85, "spo2_valid": True,
                "gsr_conductance_us": 5.0}
        active, reasons = b.check_and_alert(data)
        assert active
        assert any("SPO2_LOW" in r for r in reasons)

    def test_gsr_high_triggers_alert(self):
        b = self._make_buzzer()
        data = {"heart_rate_bpm": 75, "hr_valid": True,
                "spo2_percent": 98, "spo2_valid": True,
                "gsr_conductance_us": 25.0}
        active, reasons = b.check_and_alert(data)
        assert active
        assert any("GSR_HIGH" in r for r in reasons)

    def test_normal_values_no_alert(self):
        b = self._make_buzzer()
        data = {"heart_rate_bpm": 75, "hr_valid": True,
                "spo2_percent": 98, "spo2_valid": True,
                "gsr_conductance_us": 5.0}
        active, reasons = b.check_and_alert(data)
        assert not active
        assert reasons == []

    def test_invalid_hr_not_alerted(self):
        """HR reading marked invalid (no finger) should not trigger HR alert."""
        b = self._make_buzzer()
        data = {"heart_rate_bpm": -999, "hr_valid": False,
                "spo2_percent": None, "spo2_valid": False,
                "gsr_conductance_us": 5.0}
        active, reasons = b.check_and_alert(data)
        assert not any("HR_" in r for r in reasons)

    def test_cooldown_prevents_double_alert(self):
        """Second alert within cooldown period should not re-trigger."""
        b = self._make_buzzer()
        b._last_alert_time = 9999999.0   # simulate: just alerted
        data = {"heart_rate_bpm": 130, "hr_valid": True,
                "spo2_percent": 85, "spo2_valid": True,
                "gsr_conductance_us": 25.0}
        # check_and_alert will detect conditions but _trigger should be blocked
        # We verify by checking _last_alert_time was not reset
        original_time = b._last_alert_time
        b.check_and_alert(data)
        assert b._last_alert_time == original_time   # not updated = cooldown blocked it
