"""
sensors/bmp280_reader.py  –  BMP280 / BME280 auto-detect reader.

Reads the chip ID at startup:
  0x58 → BMP280  — temperature + pressure only
  0x60 → BME280  — temperature + pressure + humidity

Both chips share the same I2C address (0x76 / 0x77) and the same
temperature/pressure register layout.  BME280 adds humidity registers.

No external library required — smbus2 only.
"""

import logging
import struct
import time
import smbus2
from . import BaseSensor
import config

logger = logging.getLogger(__name__)

# ── Register map (shared BMP280 / BME280) ──────────────────────────────
_REG_CHIP_ID    = 0xD0
_REG_RESET      = 0xE0
_REG_STATUS     = 0xF3
_REG_CTRL_HUM   = 0xF2   # BME280 only — must write BEFORE ctrl_meas
_REG_CTRL_MEAS  = 0xF4
_REG_CONFIG     = 0xF5
_REG_PRESS_MSB  = 0xF7   # 0xF7..0xF9  pressure (20-bit)
_REG_TEMP_MSB   = 0xFA   # 0xFA..0xFC  temperature (20-bit)
_REG_HUM_MSB    = 0xFD   # 0xFD..0xFE  humidity (16-bit) — BME280 only
_REG_CALIB_00   = 0x88   # T1-T3, P1-P9 trimming (24 bytes)
_REG_CALIB_H1   = 0xA1   # dig_H1  (1 byte, unsigned) — BME280 only
_REG_CALIB_H2   = 0xE1   # dig_H2..H6 (7 bytes)       — BME280 only

_CHIP_ID_BMP280 = 0x58
_CHIP_ID_BME280 = 0x60

# ctrl_meas: osrs_t=x4, osrs_p=x4, mode=forced → 0x6D
_CTRL_MEAS_FORCED = 0x6D
# ctrl_hum:  osrs_h=x4 → 0x03
_CTRL_HUM_X4      = 0x03
# config:    no IIR filter, no standby → 0x00
_CONFIG_VAL       = 0x00


class BMP280Reader(BaseSensor):
    """
    Transparent driver for BMP280 and BME280.
    After calibrate(), self.is_bme280 tells you which chip was found.
    read() always returns 'humidity_percent'; it is None for BMP280.
    """

    name = "bmp280"

    def __init__(self):
        self._bus: smbus2.SMBus | None = None
        self._last_error: str | None = None
        self.is_bme280: bool = False   # set during calibrate()
        self.chip_id:   int  = 0

        # Temperature / pressure trimming
        self._dig_T1 = self._dig_T2 = self._dig_T3 = 0
        self._dig_P1 = self._dig_P2 = self._dig_P3 = 0
        self._dig_P4 = self._dig_P5 = self._dig_P6 = 0
        self._dig_P7 = self._dig_P8 = self._dig_P9 = 0

        # Humidity trimming (BME280 only)
        self._dig_H1 = self._dig_H2 = self._dig_H3 = 0
        self._dig_H4 = self._dig_H5 = self._dig_H6 = 0

    # ── Calibration / Setup ───────────────────────────────────────

    def calibrate(self) -> None:
        """Open I2C bus, auto-detect chip, load trimming coefficients."""
        try:
            self._bus = smbus2.SMBus(config.I2C_BUS)

            # Read and identify chip
            self.chip_id = self._bus.read_byte_data(config.BMP280_I2C_ADDRESS, _REG_CHIP_ID)
            if self.chip_id == _CHIP_ID_BME280:
                self.is_bme280 = True
                logger.info("BME280 detected (chip ID 0x60) — humidity enabled.")
            elif self.chip_id == _CHIP_ID_BMP280:
                self.is_bme280 = False
                logger.info("BMP280 detected (chip ID 0x58) — no humidity sensor.")
            else:
                raise RuntimeError(
                    f"Unknown chip ID 0x{self.chip_id:02X} at address "
                    f"0x{config.BMP280_I2C_ADDRESS:02X}. "
                    f"Expected 0x58 (BMP280) or 0x60 (BME280). "
                    "Check wiring and I2C address in config.py."
                )

            # Soft reset
            self._bus.write_byte_data(config.BMP280_I2C_ADDRESS, _REG_RESET, 0xB6)
            time.sleep(0.01)

            # Write config register (no IIR filter)
            self._bus.write_byte_data(config.BMP280_I2C_ADDRESS, _REG_CONFIG, _CONFIG_VAL)

            # For BME280: configure humidity oversampling BEFORE ctrl_meas
            if self.is_bme280:
                self._bus.write_byte_data(
                    config.BMP280_I2C_ADDRESS, _REG_CTRL_HUM, _CTRL_HUM_X4
                )

            # Load trimming coefficients
            self._load_tp_calibration()
            if self.is_bme280:
                self._load_humidity_calibration()

            # Quick test read
            sample = self.read()
            chip_name = "BME280" if self.is_bme280 else "BMP280"
            if self.is_bme280:
                logger.info(
                    "%s init reading: %.2f°C, %.2fhPa, %.2f%%RH",
                    chip_name,
                    sample.get("temperature_celsius"),
                    sample.get("pressure_hpa"),
                    sample.get("humidity_percent"),
                )
            else:
                logger.info(
                    "%s init reading: %.2f°C, %.2fhPa",
                    chip_name,
                    sample.get("temperature_celsius"),
                    sample.get("pressure_hpa"),
                )
            self._last_error = None

        except Exception as exc:
            self._last_error = str(exc)
            if self._bus:
                self._bus.close()
                self._bus = None
            logger.error("BMP/BME280 calibrate failed: %s", exc)
            raise

    def _load_tp_calibration(self) -> None:
        """Read 24 bytes of temp/pressure trimming from 0x88."""
        raw = self._bus.read_i2c_block_data(config.BMP280_I2C_ADDRESS, _REG_CALIB_00, 24)
        (
            self._dig_T1, self._dig_T2, self._dig_T3,
            self._dig_P1, self._dig_P2, self._dig_P3,
            self._dig_P4, self._dig_P5, self._dig_P6,
            self._dig_P7, self._dig_P8, self._dig_P9,
        ) = struct.unpack_from("<HhhHhhhhhhhh", bytes(raw))
        logger.debug("Temp/pressure trimming loaded. T1=%d", self._dig_T1)

    def _load_humidity_calibration(self) -> None:
        """Read BME280 humidity trimming coefficients (0xA1, 0xE1..0xE7)."""
        self._dig_H1 = self._bus.read_byte_data(config.BMP280_I2C_ADDRESS, _REG_CALIB_H1)

        h = self._bus.read_i2c_block_data(config.BMP280_I2C_ADDRESS, _REG_CALIB_H2, 7)
        self._dig_H2 = (h[1] << 8) | h[0]                          # signed 16-bit
        if self._dig_H2 >= 0x8000:
            self._dig_H2 -= 0x10000
        self._dig_H3 = h[2]                                         # unsigned 8-bit
        self._dig_H4 = (h[3] << 4) | (h[4] & 0x0F)                 # signed 12-bit
        if self._dig_H4 >= 0x800:
            self._dig_H4 -= 0x1000
        self._dig_H5 = (h[5] << 4) | (h[4] >> 4)                   # signed 12-bit
        if self._dig_H5 >= 0x800:
            self._dig_H5 -= 0x1000
        self._dig_H6 = h[6]                                         # signed 8-bit
        if self._dig_H6 >= 0x80:
            self._dig_H6 -= 0x100
        logger.debug("BME280 humidity trimming loaded. H1=%d H2=%d", self._dig_H1, self._dig_H2)

    # ── Read ──────────────────────────────────────────────────────────

    def read(self) -> dict:
        """
        Trigger a forced-mode reading and return compensated values.

        Returns
        -------
        dict with keys:
          temperature_celsius, pressure_hpa   — always present
          humidity_percent                     — float for BME280, None for BMP280
        """
        if self._bus is None:
            raise RuntimeError("Reader not calibrated; call calibrate() first.")

        try:
            # BME280: ctrl_hum must stay set; re-assert before triggering
            if self.is_bme280:
                self._bus.write_byte_data(
                    config.BMP280_I2C_ADDRESS, _REG_CTRL_HUM, _CTRL_HUM_X4
                )

            # Trigger forced measurement
            self._bus.write_byte_data(
                config.BMP280_I2C_ADDRESS, _REG_CTRL_MEAS, _CTRL_MEAS_FORCED
            )

            # Poll until measurement complete (max ~10 ms at x4 oversampling)
            for _ in range(20):
                time.sleep(0.002)
                status = self._bus.read_byte_data(config.BMP280_I2C_ADDRESS, _REG_STATUS)
                if not (status & 0x08):
                    break

            # Burst read 0xF7..0xFC: press[3] + temp[3]
            data = self._bus.read_i2c_block_data(config.BMP280_I2C_ADDRESS, _REG_PRESS_MSB, 6)
            adc_P = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
            adc_T = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)

            temp_c, t_fine = self._compensate_temp(adc_T)
            press_hpa      = self._compensate_pressure(adc_P, t_fine)

            humidity: float | None = None
            if self.is_bme280:
                hum_raw = self._bus.read_i2c_block_data(
                    config.BMP280_I2C_ADDRESS, _REG_HUM_MSB, 2
                )
                adc_H   = (hum_raw[0] << 8) | hum_raw[1]
                humidity = round(self._compensate_humidity(adc_H, t_fine), 2)

            self._last_error = None
            result = {
                "temperature_celsius": round(temp_c,    2),
                "pressure_hpa":        round(press_hpa, 2),
                "humidity_percent":    humidity,
            }
            logger.debug("BMP/BME280: %s", result)
            return result

        except Exception as exc:
            self._last_error = str(exc)
            logger.error("BMP/BME280 read error: %s", exc)
            return {
                "temperature_celsius": None,
                "pressure_hpa":        None,
                "humidity_percent":    None,
            }

    # ── Compensation formulas ─────────────────────────────────────────

    def _compensate_temp(self, adc_T: int) -> tuple[float, float]:
        """Return (temperature_celsius, t_fine)."""
        var1 = (adc_T / 16384.0 - self._dig_T1 / 1024.0) * self._dig_T2
        var2 = (adc_T / 131072.0 - self._dig_T1 / 8192.0) ** 2 * self._dig_T3
        t_fine = var1 + var2
        return t_fine / 5120.0, t_fine

    def _compensate_pressure(self, adc_P: int, t_fine: float) -> float:
        """Return pressure in hPa."""
        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self._dig_P6 / 32768.0
        var2 = var2 + var1 * self._dig_P5 * 2.0
        var2 = var2 / 4.0 + self._dig_P4 * 65536.0
        var1 = (self._dig_P3 * var1 * var1 / 524288.0 + self._dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self._dig_P1
        if var1 == 0.0:
            return 0.0
        p = 1048576.0 - adc_P
        p = ((p - var2 / 4096.0) * 6250.0) / var1
        var1 = self._dig_P9 * p * p / 2147483648.0
        var2 = p * self._dig_P8 / 32768.0
        return (p + (var1 + var2 + self._dig_P7) / 16.0) / 100.0   # Pa → hPa

    def _compensate_humidity(self, adc_H: int, t_fine: float) -> float:
        """Return relative humidity % (BME280 datasheet §4.2.3)."""
        v = t_fine - 76800.0
        v = (adc_H - (self._dig_H4 * 64.0 + (self._dig_H5 / 16384.0) * v)) * \
            (self._dig_H2 / 65536.0 * (1.0 + self._dig_H6 / 67108864.0 * v *
             (1.0 + self._dig_H3 / 67108864.0 * v)))
        v = v * (1.0 - self._dig_H1 * v / 524288.0)
        return max(0.0, min(100.0, v))

    # ── Close / Health ────────────────────────────────────────────────

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None
        logger.info("BMP/BME280 closed.")

    def health(self) -> dict:
        chip_name = "BME280" if self.is_bme280 else "BMP280" if self.chip_id else "unknown"
        return {
            "sensor":     f"{self.name} ({chip_name})",
            "ok":         self._bus is not None,
            "last_error": self._last_error,
        }


import logging
import struct
import time
import smbus2
from . import BaseSensor
import config

logger = logging.getLogger(__name__)

# ── BMP280 Register Map ───────────────────────────────────────────────────────
_REG_CHIP_ID    = 0xD0   # Should read 0x58
_REG_RESET      = 0xE0   # Write 0xB6 to soft-reset
_REG_STATUS     = 0xF3
_REG_CTRL_MEAS  = 0xF4   # osrs_t[7:5], osrs_p[4:2], mode[1:0]
_REG_CONFIG     = 0xF5   # t_sb[7:5], filter[4:2], (spi3w_en)[0]
_REG_PRESS_MSB  = 0xF7   # 0xF7 .. 0xF9  (20-bit raw pressure)
_REG_TEMP_MSB   = 0xFA   # 0xFA .. 0xFC  (20-bit raw temperature)
_REG_CALIB_00   = 0x88   # calib00 .. calib25 (trimming parameters T1-T3, P1-P9)

_CHIP_ID_BMP280 = 0x58

# ctrl_meas: oversampling x4 for both temp+pressure, forced mode
# osrs_t=011 (x4), osrs_p=011 (x4), mode=01 (forced) → 0b011_011_01 = 0x6D
# After forced read the sensor goes back to sleep; we re-trigger each read.
_CTRL_MEAS_FORCED = 0x6D   # osrs_t=x4, osrs_p=x4, mode=forced

# config: no IIR filter, no standby (filter=000, t_sb=000) → 0x00
_CONFIG_VAL = 0x00


class BMP280Reader(BaseSensor):
    name = "bmp280"

    def __init__(self):
        self._bus: smbus2.SMBus | None = None
        self._last_error: str | None = None

        # Trimming (calibration) coefficients
        self._dig_T1: int = 0
        self._dig_T2: int = 0
        self._dig_T3: int = 0
        self._dig_P1: int = 0
        self._dig_P2: int = 0
        self._dig_P3: int = 0
        self._dig_P4: int = 0
        self._dig_P5: int = 0
        self._dig_P6: int = 0
        self._dig_P7: int = 0
        self._dig_P8: int = 0
        self._dig_P9: int = 0

    # ── Calibration / Setup ──────────────────────────────────────────────

    def calibrate(self) -> None:
        """Open I2C bus, verify chip ID, load trimming coefficients."""
        try:
            self._bus = smbus2.SMBus(config.I2C_BUS)

            # Verify chip
            chip_id = self._bus.read_byte_data(config.BMP280_I2C_ADDRESS, _REG_CHIP_ID)
            if chip_id != _CHIP_ID_BMP280:
                raise RuntimeError(
                    f"BMP280 chip ID mismatch: expected 0x{_CHIP_ID_BMP280:02X}, "
                    f"got 0x{chip_id:02X}. Check wiring & I2C address in config.py."
                )

            # Soft reset
            self._bus.write_byte_data(config.BMP280_I2C_ADDRESS, _REG_RESET, 0xB6)
            time.sleep(0.01)

            # Write config (no IIR filter) then ctrl_meas is set per-read in forced mode
            self._bus.write_byte_data(config.BMP280_I2C_ADDRESS, _REG_CONFIG, _CONFIG_VAL)

            # Load factory trimming coefficients
            self._load_calibration()

            # Quick test read
            sample = self.read()
            logger.info(
                "BMP280 detected OK — init reading: %.2f°C, %.2fhPa",
                sample.get("temperature_celsius"), sample.get("pressure_hpa"),
            )
            self._last_error = None

        except Exception as exc:
            self._last_error = str(exc)
            if self._bus:
                self._bus.close()
                self._bus = None
            logger.error("BMP280 calibrate failed: %s", exc)
            raise

    def _load_calibration(self) -> None:
        """Read 24 bytes of trimming coefficients from 0x88-0x9F."""
        raw = self._bus.read_i2c_block_data(
            config.BMP280_I2C_ADDRESS, _REG_CALIB_00, 24
        )
        # Unpack: T1 unsigned, T2/T3 signed, P1 unsigned, P2-P9 signed
        (self._dig_T1,
         self._dig_T2,
         self._dig_T3,
         self._dig_P1,
         self._dig_P2,
         self._dig_P3,
         self._dig_P4,
         self._dig_P5,
         self._dig_P6,
         self._dig_P7,
         self._dig_P8,
         self._dig_P9) = struct.unpack_from("<HhhHhhhhhhhh", bytes(raw))
        logger.debug("BMP280 trimming: T1=%d T2=%d T3=%d", self._dig_T1, self._dig_T2, self._dig_T3)

    # ── Read ─────────────────────────────────────────────────────────────

    def read(self) -> dict:
        """
        Trigger a forced-mode reading and return compensated values.

        Returns
        -------
        dict with keys: temperature_celsius, pressure_hpa
        """
        if self._bus is None:
            raise RuntimeError("BMP280Reader not calibrated; call calibrate() first.")

        try:
            # Trigger forced measurement
            self._bus.write_byte_data(
                config.BMP280_I2C_ADDRESS, _REG_CTRL_MEAS, _CTRL_MEAS_FORCED
            )

            # Wait for measurement to complete (~8 ms for x4 oversampling)
            for _ in range(10):
                time.sleep(0.01)
                status = self._bus.read_byte_data(config.BMP280_I2C_ADDRESS, _REG_STATUS)
                if not (status & 0x08):   # measuring bit = 0 → done
                    break

            # Burst read pressure (3 bytes) + temperature (3 bytes) from 0xF7..0xFC
            data = self._bus.read_i2c_block_data(config.BMP280_I2C_ADDRESS, _REG_PRESS_MSB, 6)

            adc_P = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
            adc_T = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)

            temp_c, t_fine = self._compensate_temp(adc_T)
            press_hpa      = self._compensate_pressure(adc_P, t_fine)

            self._last_error = None
            result = {
                "temperature_celsius": round(temp_c,   2),
                "pressure_hpa":        round(press_hpa, 2),
            }
            logger.debug("BMP280: %s", result)
            return result

        except Exception as exc:
            self._last_error = str(exc)
            logger.error("BMP280 read error: %s", exc)
            return {
                "temperature_celsius": None,
                "pressure_hpa":        None,
            }

    # ── Compensation formulas (BMP280 datasheet §4.2.3) ──────────────────

    def _compensate_temp(self, adc_T: int) -> tuple[float, float]:
        """Return (temperature_celsius, t_fine). t_fine is needed for pressure."""
        var1 = (adc_T / 16384.0 - self._dig_T1 / 1024.0) * self._dig_T2
        var2 = (adc_T / 131072.0 - self._dig_T1 / 8192.0) ** 2 * self._dig_T3
        t_fine = var1 + var2
        return t_fine / 5120.0, t_fine

    def _compensate_pressure(self, adc_P: int, t_fine: float) -> float:
        """Return pressure in hPa."""
        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self._dig_P6 / 32768.0
        var2 = var2 + var1 * self._dig_P5 * 2.0
        var2 = var2 / 4.0 + self._dig_P4 * 65536.0
        var1 = (self._dig_P3 * var1 * var1 / 524288.0 + self._dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self._dig_P1
        if var1 == 0.0:
            return 0.0        # avoid division by zero
        p = 1048576.0 - adc_P
        p = ((p - var2 / 4096.0) * 6250.0) / var1
        var1 = self._dig_P9 * p * p / 2147483648.0
        var2 = p * self._dig_P8 / 32768.0
        p = p + (var1 + var2 + self._dig_P7) / 16.0
        return p / 100.0      # Pa → hPa

    # ── Close / Health ────────────────────────────────────────────────────

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None
        logger.info("BMP280 closed.")

    def health(self) -> dict:
        return {
            "sensor":     self.name,
            "ok":         self._bus is not None,
            "last_error": self._last_error,
        }
