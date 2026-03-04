"""
sensors/hrcalc.py  –  Heart rate & SpO2 calculation from MAX30102 FIFO data.
Port of Maxim Integrated's reference algorithm (originally in C/Arduino).
"""

import numpy as np


# ── SpO2 lookup table (Maxim reference Table 2) ──────────────────────────────
# Indexed by R ratio * 100 (0–184), entries are SpO2 %
_SPO2_TABLE = [
    100, 100, 100, 100, 99, 99, 99, 99, 99, 99,
     99,  98, 98,  98,  98, 98, 97, 97, 97, 97,
     97,  97, 96,  96,  96, 96, 96, 96, 95, 95,
     95,  95, 95,  95,  94, 94, 94, 94, 94, 93,
     93,  93, 93,  93,  92, 92, 92, 92, 92, 91,
     91,  91, 91,  90,  90, 90, 90, 89, 89, 89,
     89,  88, 88,  88,  88, 87, 87, 87, 87, 86,
     86,  86, 86,  85,  85, 85, 84, 84, 84, 84,
     83,  83, 83,  82,  82, 82, 82, 81, 81, 81,
     80,  80, 80,  79,  79, 79, 78, 78, 78, 77,
     77,  77, 76,  76,  76, 75, 75, 74, 74, 74,
     73,  73, 72,  72,  72, 71, 71, 70, 70, 69,
     69,  69, 68,  68,  67, 67, 66, 66, 65, 65,
     64,  64, 63,  63,  62, 62, 61, 61, 60, 60,
     59,  59, 58,  57,  57, 56, 56, 55, 54, 54,
     53,  52, 52,  51,  51, 50, 49, 48, 48, 47,
     46,  46, 45,  44,  43, 43, 42, 41, 40, 40,
     39,  38, 37,  36,  35, 35, 34, 33, 32, 31,
     30,  29, 28,  27,  26, 25, 23, 22, 21, 20,
]


def calc_hr_and_spo2(
    ir_data: list,
    red_data: list,
    sampling_freq: int = 100,
) -> tuple[float, bool, float, bool]:
    """
    Calculate heart rate (BPM) and SpO2 (%) from a buffer of IR + Red samples.

    Parameters
    ----------
    ir_data : list[int]
        ~100 IR samples from MAX30102 FIFO.
    red_data : list[int]
        ~100 Red samples (same length as ir_data).
    sampling_freq : int
        Effective sampling frequency in Hz after any hardware averaging.
        For SMP_AVE=4 @ 400 Hz, effective = 100 Hz.

    Returns
    -------
    (heart_rate_bpm, hr_valid, spo2_percent, spo2_valid)
    """
    ir  = np.array(ir_data,  dtype=np.float64)
    red = np.array(red_data, dtype=np.float64)
    n   = len(ir)

    if n < 50:
        return -999.0, False, -999.0, False

    # ── 1. Remove DC (mean-difference) ─────────────────────────────────────
    ir_mean  = np.mean(ir)
    red_mean = np.mean(red)

    if ir_mean < 5000:
        # No finger on sensor (or finger not properly placed)
        import logging as _log
        _log.getLogger(__name__).debug(
            "MAX30102: no finger detected (ir_mean=%.0f < 5000)", ir_mean
        )
        return -999.0, False, -999.0, False

    ir_ac  = ir  - ir_mean
    red_ac = red - red_mean

    # ── 2. Peak detection on AC-IR ─────────────────────────────────────────
    #    Find local maxima with minimum prominence
    peaks = _find_peaks(ir_ac, min_distance=int(sampling_freq * 0.33))

    if len(peaks) < 2:
        return -999.0, False, -999.0, False

    # ── 3. Heart rate from inter-peak intervals ─────────────────────────────
    intervals = np.diff(peaks)  # in samples
    avg_interval_samples = np.mean(intervals)
    hr_bpm = (sampling_freq / avg_interval_samples) * 60.0
    hr_valid = 30 <= hr_bpm <= 250

    # ── 4. SpO2 via ratio-of-ratios (cycle-by-cycle peak-to-trough) ──────────
    #    R = (AC_red/DC_red) / (AC_ir/DC_ir)
    #    Use per-cycle amplitude instead of std() to reject motion noise.
    #    Peaks found in ir_ac; trough is the min between consecutive peaks.
    cycle_ac_ir:  list[float] = []
    cycle_ac_red: list[float] = []
    for i in range(1, len(peaks)):
        seg_ir  = ir_ac[peaks[i - 1]:peaks[i] + 1]
        seg_red = red_ac[peaks[i - 1]:peaks[i] + 1]
        pp_ir   = float(ir_ac[peaks[i]] - np.min(seg_ir))
        pp_red  = float(red_ac[peaks[i]] - np.min(seg_red))
        if pp_ir > 0 and pp_red > 0:
            cycle_ac_ir.append(pp_ir)
            cycle_ac_red.append(pp_red)

    if not cycle_ac_ir or ir_mean < 1.0 or red_mean < 1.0:
        return hr_bpm, hr_valid, -999.0, False

    ac_ir  = float(np.mean(cycle_ac_ir))
    ac_red = float(np.mean(cycle_ac_red))

    if ac_ir < 1.0:
        return hr_bpm, hr_valid, -999.0, False

    r = (ac_red / red_mean) / (ac_ir / ir_mean)
    r_idx = max(0, min(int(r * 100), len(_SPO2_TABLE) - 1))
    spo2  = float(_SPO2_TABLE[r_idx])
    spo2_valid = hr_valid and (70.0 <= spo2 <= 100.0)

    return round(hr_bpm, 1), hr_valid, spo2, spo2_valid


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_peaks(signal: np.ndarray, min_distance: int = 30) -> list:
    """
    Simple local-maxima detector with minimum distance constraint.
    Returns indices of detected peaks.
    """
    peaks = []
    n = len(signal)
    i = 1
    while i < n - 1:
        if signal[i] > signal[i - 1] and signal[i] > signal[i + 1]:
            if signal[i] > 0:  # only positive peaks
                if not peaks or (i - peaks[-1]) >= min_distance:
                    peaks.append(i)
        i += 1
    return peaks
