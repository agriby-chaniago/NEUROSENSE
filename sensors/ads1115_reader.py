"""
sensors/ads1115_reader.py  –  Dual ADS1115 16-bit ADC reader via I2C (smbus2).

Supports two ADS1115 units:
  Unit 1 — ADDR pin → GND → I2C address 0x48
  Unit 2 — ADDR pin → VCC → I2C address 0x49

Each unit has 4 single-ended channels (AIN0–AIN3).
Readings are returned as voltages (V) using ±4.096 V PGA gain.

To connect an analog sensor, wire its output to one of AIN0–AIN3 and
reference it by name in config.ADS1115_CHANNELS.

Wiring (both units):
  VDD → 3.3 V  (Pin 1 on Pi)
  GND → GND    (Pin 6 / Pin 9)
  SDA → GPIO2  (Pin 3)
  SCL → GPIO3  (Pin 5)
  Unit 1: ADDR → GND          → address 0x48
  Unit 2: ADDR → VDD (3.3V)   → address 0x49
  ALRT pin → not connected (comparator not used)
"""

import logging
import time
import smbus2
from . import BaseSensor
import config

logger = logging.getLogger(__name__)

# ── ADS1115 Register Addresses ─────────────────────────────────────────────
_REG_CONVERSION = 0x00
_REG_CONFIG     = 0x01

# ── Config register bit fields ─────────────────────────────────────────────
# Bits 15-8 (high byte):  OS | MUX[2:0] | PGA[2:0] | MODE
# Bits  7-0 (low byte):   DR[2:0] | COMP_MODE | COMP_POL | COMP_LAT | COMP_QUE[1:0]
#
# MUX for single-ended:  AIN0=0b100, AIN1=0b101, AIN2=0b110, AIN3=0b111
# PGA = 0b001 → ±4.096 V  (1 LSB = 125 µV at full 16-bit)
# MODE = 1 → single-shot
# DR = 0b111 → 860 SPS (fastest)
# COMP_QUE = 0b11 → disable comparator

_PGA_4096V = 0b001    # ±4.096 V — safe for 3.3 V sensors
_VOLTS_PER_LSB = 4.096 / 32768.0   # ≈ 125 µV

_MUX_CHANNELS = {
    0: 0b100,
    1: 0b101,
    2: 0b110,
    3: 0b111,
}


def _build_config(channel: int) -> tuple[int, int]:
    """Return (high_byte, low_byte) for the ADS1115 config register."""
    mux  = _MUX_CHANNELS[channel]
    high = (0b1 << 7)                     # OS = 1 (start conversion), bit 15→7 in byte
    high |= (mux  << 4)                   # MUX[2:0] → bits 14-12 → byte bits 6-4
    high |= (_PGA_4096V << 1)             # PGA[2:0] → bits 11-9  → byte bits 3-1
    high |= 0b1                           # MODE = 1 (single-shot)
    low  = (0b111 << 5) | 0b11            # DR=860SPS, COMP_QUE=disabled
    return high, low


class ADS1115Reader(BaseSensor):
    """Reads voltage from two ADS1115 units (addresses 0x48 and 0x49)."""

    name = "ads1115"

    def __init__(self):
        self._bus: smbus2.SMBus | None = None
        self._last_error: str | None = None
        # Which (address, channel, label) tuples to read — loaded from config
        self._channels: list[tuple[int, int, str]] = []

    # ── Calibration / Setup ──────────────────────────────────────────────

    def calibrate(self) -> None:
        """Open I2C bus and verify both ADS1115 units respond."""
        try:
            self._bus = smbus2.SMBus(config.I2C_BUS)
            self._channels = config.ADS1115_CHANNELS

            active_addrs = {addr for addr, _, _ in self._channels}
            for addr in active_addrs:
                # Trigger a dummy read on CH0 to confirm the device exists
                hi, lo = _build_config(0)
                self._bus.write_i2c_block_data(addr, _REG_CONFIG, [hi, lo])
                time.sleep(0.002)
                raw = self._bus.read_i2c_block_data(addr, _REG_CONVERSION, 2)
                volts = self._raw_to_volts(raw)
                logger.info("ADS1115 @ 0x%02X detected OK — CH0 = %.4f V", addr, volts)

            self._last_error = None

        except Exception as exc:
            self._last_error = str(exc)
            if self._bus:
                self._bus.close()
                self._bus = None
            logger.error("ADS1115 calibrate failed: %s", exc)
            raise

    # ── Read ─────────────────────────────────────────────────────────────

    def read(self) -> dict:
        """
        Read all configured channels from both ADS1115 units.

        Returns
        -------
        dict  { label: voltage_V, ... }
        e.g.  { "ads1_ch0_V": 1.234, "ads2_ch0_V": 0.987, ... }
        """
        if self._bus is None:
            raise RuntimeError("ADS1115Reader not calibrated; call calibrate() first.")

        result: dict[str, float | None] = {}
        for addr, channel, label in self._channels:
            try:
                volts = self._read_channel(addr, channel)
                result[label] = round(volts, 5)
            except Exception as exc:
                logger.error("ADS1115 0x%02X ch%d error: %s", addr, channel, exc)
                result[label] = None

        self._last_error = None
        logger.debug("ADS1115: %s", result)
        return result

    def _read_channel(self, addr: int, channel: int) -> float:
        """Trigger a single-shot conversion and return voltage."""
        hi, lo = _build_config(channel)
        self._bus.write_i2c_block_data(addr, _REG_CONFIG, [hi, lo])

        # Wait for conversion (≤ 1.2 ms at 860 SPS, poll status bit)
        for _ in range(20):
            time.sleep(0.002)
            cfg = self._bus.read_i2c_block_data(addr, _REG_CONFIG, 2)
            if cfg[0] & 0x80:   # OS bit = 1 → conversion complete
                break

        raw = self._bus.read_i2c_block_data(addr, _REG_CONVERSION, 2)
        return self._raw_to_volts(raw)

    @staticmethod
    def _raw_to_volts(raw: list[int]) -> float:
        """Convert two raw bytes (big-endian signed) to volts."""
        value = (raw[0] << 8) | raw[1]
        if value >= 0x8000:   # two's complement
            value -= 0x10000
        return value * _VOLTS_PER_LSB

    # ── Close / Health ────────────────────────────────────────────────────

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None
        logger.info("ADS1115 closed.")

    def health(self) -> dict:
        return {
            "sensor":     self.name,
            "ok":         self._bus is not None,
            "last_error": self._last_error,
        }
