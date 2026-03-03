"""
NEUROSENSE – Central Configuration
All hardware addresses, GPIO pins, sampling settings, and paths live here.
Change sensor wiring? Update this file only.
"""

# ─── Schema ──────────────────────────────────────────────────────────────────
# Bump this when CSV column layout changes so old data can be distinguished.
DATA_SCHEMA_VERSION = "1.0.0"

# ─── I2C Bus ───────────────────────────────────────────────────────────────
I2C_BUS = 1   # /dev/i2c-1 (Raspberry Pi standard bus)

# ─── BME280 ────────────────────────────────────────────────────────────────
# Wiring: SDA→Pin3(GPIO2), SCL→Pin5(GPIO3), VIN→3.3V(Pin1), GND→Pin6
# Address: 0x76 when SDO→GND, 0x77 when SDO→VCC
# Verify with: i2cdetect -y 1
BME280_I2C_ADDRESS = 0x76

# ─── MAX30102 ─────────────────────────────────────────────────────────────
# Wiring: SDA→Pin3(GPIO2), SCL→Pin5(GPIO3), VIN→3.3V(Pin1), GND→Pin9
# INT pin NOT used (polling mode — avoids Pi 5 GPIO interrupt issues)
# Address: 0x57 (fixed by Maxim datasheet)
MAX30102_I2C_ADDRESS = 0x57
MAX30102_SAMPLE_BUFFER = 100  # Number of IR/Red samples per HR calculation
MAX30102_SAMPLING_RATE_HZ = 400  # Internal sensor sampling rate setting

# ─── Grove GSR Sensor ─────────────────────────────────────────────────────
# Wiring: Plug Grove cable into A0 port on Grove Base HAT
# Grove Base HAT ADC (STM32 v1.1) I2C address: 0x04
# If your HAT uses MM32 (v1.0), change to 0x08
# Verify with: i2cdetect -y 1
GROVE_HAT_ADC_ADDRESS = 0x04
GSR_GROVE_CHANNEL = 0    # A0 port = channel 0
GSR_ADC_BITS = 12        # Grove Base HAT returns 12-bit values (0–4095)

# ─── Sensor Manager ───────────────────────────────────────────────────────
# How often (seconds) each sensor thread reads a new value
BME280_INTERVAL_S   = 2.0
MAX30102_INTERVAL_S = 4.0   # Longer — needs to collect buffer of samples
GSR_INTERVAL_S      = 0.5

# ─── Data Logging ──────────────────────────────────────────────────────────
import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# CSV columns — order matters for readability
CSV_FIELDNAMES = [
    "timestamp_utc",
    "schema_version",
    "heart_rate_bpm",
    "spo2_percent",
    "hr_valid",
    "spo2_valid",
    "temperature_celsius",
    "humidity_percent",
    "pressure_hpa",
    "gsr_raw_adc",
    "gsr_resistance_ohm",
    "gsr_conductance_us",  # microSiemens (EDA signal)
    "alert_active",        # True jika ada kondisi bahaya saat pembacaan ini
    "alert_reasons",       # deskripsi kondisi bahaya, dipisah koma
]

# ─── Dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5000
DASHBOARD_SSE_INTERVAL_S = 1.0   # How often SSE pushes data to browser

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
ALERT_HR_LOW     = 45    # < 45 BPM  → bradycardia warning

# SpO2 (%)
ALERT_SPO2_LOW   = 90    # < 90% → hipoksemia bahaya

# GSR Conductance (µS) — nilai tinggi = stres/arousal tinggi
ALERT_GSR_HIGH_US = 20.0  # > 20 µS → level stres tinggi

# ─── Active Sensors ────────────────────────────────────────────────────────
# To disable a sensor, set its entry to False.
# sensor_manager reads this dict — adding a new sensor only needs a new key here
# and a corresponding reader class.
ACTIVE_SENSORS = {
    "bme280":   True,
    "max30102": True,
    "gsr":      True,
}
