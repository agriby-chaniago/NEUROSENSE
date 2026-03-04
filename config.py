"""
NEUROSENSE – Central Configuration
All hardware addresses, GPIO pins, sampling settings, and paths live here.
Change sensor wiring? Update this file only.
"""

# ─── I2C Bus ───────────────────────────────────────────────────────────────
I2C_BUS = 1   # /dev/i2c-1 (Raspberry Pi standard bus)

# ─── BMP280 (temperature + pressure, NO humidity) ────────────────────────
# Wiring:
#   VCC → 3.3V (Pin 1)    GND → GND (Pin 6)
#   SDA → GPIO2 (Pin 3)   SCL → GPIO3 (Pin 5)
#   SDO → GND             → I2C address 0x76  (SDO → 3.3V = 0x77)
#   CSB → 3.3V            → selects I2C mode (not SPI)
# Verify: i2cdetect -y 1  → should show 0x76
BMP280_I2C_ADDRESS = 0x76

# ─── MAX30102 ─────────────────────────────────────────────────────────────
# Wiring: SDA→Pin3(GPIO2), SCL→Pin5(GPIO3), VIN→3.3V(Pin1), GND→Pin9
# INT pin NOT used (polling mode — avoids Pi 5 GPIO interrupt issues)
# Address: 0x57 (fixed by Maxim datasheet)
MAX30102_I2C_ADDRESS = 0x57
# REG_SPO2_CONFIG=0x27: bits[4:2]=001 → SR=100 Hz. SMP_AVE=4 → FIFO=25 Hz.
# Empirically confirmed: 100 samples ≈ 4s → 25 Hz effective output rate.
# SMP_AVE=4 → effective rate = 100/4 = 25 Hz
# 300 samples @ 25 Hz = 12s history → 3+ cardiac cycles minimum pada 60 BPM
MAX30102_SAMPLE_BUFFER = 300
MAX30102_SAMPLING_RATE_HZ = 100  # REG_SPO2_CONFIG=0x27 bits[4:2]=001 → SR=100 Hz
# First HR/SpO2 result appears after MIN_SAMPLES are collected.
# 100 samples @ 25 Hz = 4s — enough for 5+ cardiac cycles at 80 BPM.
# After that, results update every _STEP_SIZE samples (~1s).
MAX30102_MIN_SAMPLES = 100

# ─── Grove GSR Sensor ─────────────────────────────────────────────────────
# Wiring: Plug Grove cable into A0 port on Grove Base HAT
# Grove Base HAT ADC (STM32 v1.1) I2C address: 0x04
# If your HAT uses MM32 (v1.0), change to 0x08
# Verify with: i2cdetect -y 1
GROVE_HAT_ADC_ADDRESS = 0x04
GSR_GROVE_CHANNEL = 0    # A0 port = channel 0
GSR_ADC_BITS = 12        # Grove Base HAT returns 12-bit values (0–4095)

# ─── ADS1115 (Dual 16-bit ADC via I2C) ───────────────────────────────────
# Wiring (same for both units):
#   VDD → 3.3V (Pin 1)    GND → GND (Pin 6)
#   SDA → GPIO2 (Pin 3)   SCL → GPIO3 (Pin 5)
#   ALRT pin → not connected
#   Unit 1: ADDR → GND  → address 0x48
#   Unit 2: ADDR → VDD  → address 0x49
# Verify: i2cdetect -y 1  → should show 0x48 and 0x49
ADS1115_1_ADDRESS = 0x48
ADS1115_2_ADDRESS = 0x49

# Channels to read: list of (i2c_address, channel_0_to_3, label)
# label = CSV column name. Add/remove entries to configure active channels.
# Connect analog sensors (EMG, flex, NTC, etc.) to AINx → 3.3V max input!
ADS1115_CHANNELS = [
    (0x48, 0, "ads1_ch0_V"),   # ADS1 AIN0 → connect analog sensor here
    (0x48, 1, "ads1_ch1_V"),   # ADS1 AIN1 → spare
    (0x49, 0, "ads2_ch0_V"),   # ADS2 AIN0 → connect analog sensor here
    (0x49, 1, "ads2_ch1_V"),   # ADS2 AIN1 → spare
]

# ─── Sensor Manager ───────────────────────────────────────────────────────
# How often (seconds) each sensor thread reads a new value
BMP280_INTERVAL_S   = 2.0
MAX30102_INTERVAL_S = 0.0   # read_sequential() already blocks for the buffer duration
GSR_INTERVAL_S      = 0.5
ADS1115_INTERVAL_S  = 0.5

# ─── Data Logging ──────────────────────────────────────────────────────────
import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# CSV columns — order matters for readability
# Bump DATA_SCHEMA_VERSION when this list changes (e.g. "1.1.0").
# humidity_percent removed: BMP280 does not have a humidity sensor.
DATA_SCHEMA_VERSION = "1.1.0"

CSV_FIELDNAMES = [
    "timestamp_utc",
    "schema_version",
    # ── MAX30102 ──
    "heart_rate_bpm",
    "spo2_percent",
    "hr_valid",
    "spo2_valid",
    # ── BMP280 / BME280 ──
    # humidity_percent: float for BME280, None (empty in CSV) for BMP280
    "temperature_celsius",
    "humidity_percent",
    "pressure_hpa",
    # ── Grove GSR ──
    "gsr_raw_adc",
    "gsr_resistance_ohm",
    "gsr_conductance_us",   # microSiemens (EDA signal)
    # ── ADS1115 (extend by adding channel labels from ADS1115_CHANNELS) ──
    "ads1_ch0_V",
    "ads1_ch1_V",
    "ads2_ch0_V",
    "ads2_ch1_V",
    # ── Alerts ──
    "alert_active",         # True jika ada kondisi bahaya saat pembacaan ini
    "alert_reasons",        # deskripsi kondisi bahaya, dipisah koma
]

# ─── Dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5000
DASHBOARD_SSE_INTERVAL_S = 0.5   # Push to browser every 500 ms

# ─── Grove Buzzer v1.3 ────────────────────────────────────────────────────
# Wiring: Colok ke port D5 pada Grove Base HAT
# D5 port pada Grove Base HAT = GPIO 5 Raspberry Pi
# Active buzzer: HIGH = berbunyi, LOW = diam
BUZZER_GPIO_PIN   = 5      # GPIO 5 = D5 port pada Grove Base HAT
BUZZER_GPIO_CHIP  = 4      # Raspberry Pi 5 menggunakan gpiochip4 (bukan gpiochip0!)
                           # Pi 4 / Pi 3 gunakan chip 0
BUZZER_ENABLED    = True

# Durasi bunyi untuk tiap kondisi alert (detik)
BUZZER_SHORT_BEEP_S  = 0.1   # 1 beep pendek  → warning ringan
BUZZER_LONG_BEEP_S   = 0.5   # 1 beep panjang → bahaya
BUZZER_COOLDOWN_S    = 5.0   # Jeda minimum antara alert (hindari bunyi terus-menerus)

# Test beep saat startup (2 beep pendek untuk konfirmasi buzzer bekerja)
# Nonaktifkan jika buzzer sudah terbukti berfungsi dan bunyi startup mengganggu
BUZZER_STARTUP_BEEP  = True

# ─── Alert Thresholds ─────────────────────────────────────────────────────
# Buzzer akan berbunyi jika nilai sensor melewati batas ini.
# Set ke None untuk menonaktifkan threshold tertentu.

# Heart Rate (BPM)
ALERT_HR_HIGH    = 120   # > 120 BPM → tachycardia warning
# 40 BPM = lag_max boundary — hampir semua baca ≤40 BPM adalah artifact lag_max,
# bukan bradycardia nyata. Turunkan threshold agar tidak false-alert.
ALERT_HR_LOW     = 40    # < 40 BPM  → bradycardia berat

# SpO2 (%)
# Sensor MAX30102 memberikan pembacaan ~4-6% lebih rendah dari nilai sebenarnya
# (offset kalibrasi). Gunakan 85% sebagai threshold bahaya nyata, bukan 90%.
ALERT_SPO2_LOW   = 85    # < 85% → hipoksemia berbahaya (kalibrasi offset ~5%)

# GSR Conductance (µS) — nilai tinggi = stres/arousal tinggi
ALERT_GSR_HIGH_US = 20.0  # > 20 µS → level stres tinggi

# ─── Active Sensors ────────────────────────────────────────────────────────
# To disable a sensor, set its entry to False.
# sensor_manager reads this dict — adding a new sensor only needs a new key here
# and a corresponding reader class.
ACTIVE_SENSORS = {
    "bmp280":   True,
    "max30102": True,
    "gsr":      True,
    "ads1115":  False,  # Set True setelah ADS1115 tersambung ke Pi
}
