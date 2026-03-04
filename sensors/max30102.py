"""
sensors/max30102.py  –  Low-level MAX30102 driver for Raspberry Pi
Port of vrano714's RPi MAX30102 driver (MIT licence).
Uses smbus2 (I2C) and polling mode — no GPIO INT pin required (Pi 5 safe).
"""

import time
import smbus2

# ─── Register Map (Maxim datasheet Table 1) ──────────────────────────────────
REG_INTR_STATUS_1   = 0x00
REG_INTR_STATUS_2   = 0x01
REG_INTR_ENABLE_1   = 0x02
REG_INTR_ENABLE_2   = 0x03

REG_FIFO_WR_PTR     = 0x04
REG_OVF_COUNTER     = 0x05
REG_FIFO_RD_PTR     = 0x06
REG_FIFO_DATA       = 0x07

REG_FIFO_CONFIG     = 0x08
REG_MODE_CONFIG     = 0x09
REG_SPO2_CONFIG     = 0x0A

REG_LED1_PA         = 0x0C   # Red LED pulse amplitude
REG_LED2_PA         = 0x0D   # IR  LED pulse amplitude
REG_PILOT_PA        = 0x10

REG_MULTI_LED_CTRL1 = 0x11
REG_MULTI_LED_CTRL2 = 0x12

REG_TEMP_INTR       = 0x1F
REG_TEMP_FRAC       = 0x20
REG_TEMP_CONFIG     = 0x21

REG_REV_ID          = 0xFE
REG_PART_ID         = 0xFF

# ─── Default values ──────────────────────────────────────────────────────────
EXPECTED_PART_ID    = 0x15
MAX_BUFFER_SIZE     = 32   # FIFO depth on device


class MAX30102:
    """
    Low-level interface for the MAX30102 pulse oximeter / heart rate sensor.

    Parameters
    ----------
    i2c_bus : int
        I2C bus number (default 1 on Raspberry Pi).
    address : int
        I2C device address (default 0x57, fixed by Maxim).
    """

    def __init__(self, i2c_bus: int = 1, address: int = 0x57):
        self._bus = smbus2.SMBus(i2c_bus)
        self._addr = address
        self._reset()
        self._setup()

    # ── Setup / teardown ──────────────────────────────────────────────────

    def _reset(self):
        """Soft-reset the device and wait for it to come back up."""
        self._write(REG_MODE_CONFIG, 0x40)
        time.sleep(0.1)

    def _setup(self):
        """
        Configure for SpO2 mode (LED1=Red, LED2=IR).
        Settings mirror Maxim's recommended defaults for finger measurement.
        """
        # Interrupt: FIFO almost-full at 17 samples
        self._write(REG_INTR_ENABLE_1, 0xC0)
        self._write(REG_INTR_ENABLE_2, 0x00)

        # FIFO: sample averaging=4, FIFO rollover off, almost-full=17
        self._write(REG_FIFO_WR_PTR, 0x00)
        self._write(REG_OVF_COUNTER, 0x00)
        self._write(REG_FIFO_RD_PTR, 0x00)
        self._write(REG_FIFO_CONFIG, 0x4F)   # SMP_AVE=4, FIFO_ROLLOVER=0, FIFO_A_FULL=15

        # SpO2 mode (LED1+LED2), 18-bit ADC, 400 Hz sample rate
        self._write(REG_MODE_CONFIG, 0x03)    # SPO2 mode
        self._write(REG_SPO2_CONFIG, 0x27)    # ADC_RGE=4096nA, SR=400Hz, LED_PW=411µs (18-bit)

        # LED pulse amplitudes — 0x24 = 36 × 0.2mA = 7.2mA each.
        # At 18-bit ADC full-scale (262143) with 25mA the signal saturates.
        # 7.2mA gives ir_mean ~50k–120k with finger properly placed (ideal range).
        self._write(REG_LED1_PA, 0x24)        # Red  ~7.2 mA
        self._write(REG_LED2_PA, 0x24)        # IR   ~7.2 mA
        self._write(REG_PILOT_PA, 0x7F)

    # ── I2C primitives ───────────────────────────────────────────────────

    def _write(self, register: int, value: int):
        self._bus.write_byte_data(self._addr, register, value)

    def _read(self, register: int) -> int:
        return self._bus.read_byte_data(self._addr, register)

    def _read_block(self, register: int, length: int) -> list:
        return self._bus.read_i2c_block_data(self._addr, register, length)

    # ── FIFO ─────────────────────────────────────────────────────────────

    def _read_fifo(self) -> tuple[int, int]:
        """
        Read one Red + IR sample (6 bytes) from the FIFO.

        Returns
        -------
        tuple[int, int]
            (red_sample, ir_sample) 18-bit values (0 – 262143)
        """
        raw = self._read_block(REG_FIFO_DATA, 6)
        red = ((raw[0] & 0x03) << 16) | (raw[1] << 8) | raw[2]
        ir  = ((raw[3] & 0x03) << 16) | (raw[4] << 8) | raw[5]
        return red, ir

    def _get_fifo_count(self) -> int:
        """Return how many unread samples are in the FIFO."""
        wr = self._read(REG_FIFO_WR_PTR)
        rd = self._read(REG_FIFO_RD_PTR)
        count = (wr - rd) & 0x1F
        return count

    # ── Public API ───────────────────────────────────────────────────────

    def read_sequential(self, sample_count: int = 100) -> tuple[list, list]:
        """
        Blocking read — collects `sample_count` Red + IR samples via polling.
        No INT pin needed (Pi 5 safe).

        Parameters
        ----------
        sample_count : int
            Number of samples to collect (100 recommended for HR algorithm).

        Returns
        -------
        tuple[list, list]
            (red_samples, ir_samples)
        """
        red_buf = []
        ir_buf  = []

        while len(red_buf) < sample_count:
            count = self._get_fifo_count()
            for _ in range(count):
                red, ir = self._read_fifo()
                red_buf.append(red)
                ir_buf.append(ir)
                if len(red_buf) >= sample_count:
                    break
            if len(red_buf) < sample_count:
                time.sleep(0.01)   # ~10ms poll interval

        return red_buf[:sample_count], ir_buf[:sample_count]

    def get_part_id(self) -> int:
        """Read PART_ID register — should return 0x15 for MAX30102."""
        return self._read(REG_PART_ID)

    def close(self):
        """Release I2C bus."""
        try:
            self._bus.close()
        except Exception:
            pass
