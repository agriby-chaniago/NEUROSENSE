"""
sensors/hrcalc.py  –  Heart rate & SpO2 calculation from MAX30102 FIFO data.
Port of Maxim Integrated's reference algorithm (originally in C/Arduino).
"""

import numpy as np
import logging

_log = logging.getLogger(__name__)


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
        _log.warning(
            "MAX30102: no finger / weak signal (ir_mean=%.0f, need >5000)", ir_mean
        )
        return -999.0, False, -999.0, False

    ir_ac  = ir  - ir_mean
    red_ac = red - red_mean

    # ── Motion-artifact guard ───────────────────────────────────────────────
    #    If the AC RMS is > 15 % of DC the buffer is dominated by movement,
    #    not pulsatile flow (placement/removal shows PI 20-160 %, motion ~10 %).
    #    Normal PPG: AC RMS / DC ≈ 0.04 – 5 %.
    ac_rms_ratio = float(np.std(ir_ac)) / (ir_mean + 1e-9)
    if ac_rms_ratio > 0.15:
        _log.warning(
            "MAX30102 motion artifact: AC_RMS/DC=%.1f%% (need ≤15%%), skipping buffer",
            ac_rms_ratio * 100,
        )
        return -999.0, False, -999.0, False

    # ── Linear detrend ──────────────────────────────────────────────────────
    #    Respiration (~0.2–0.3 Hz) causes a slow sinusoidal baseline drift that
    #    survives the mean subtraction.  With only ~2 cardiac cycles in 200 s,
    #    this drift makes the second half of the buffer anti-correlate with the
    #    first half → negative normalised autocorr even for a real PPG signal.
    #    A linear fit + subtraction removes the dominant slope component cheaply.
    t = np.arange(n, dtype=np.float64)
    ir_slope  = np.polyfit(t, ir_ac,  1)
    red_slope = np.polyfit(t, red_ac, 1)
    ir_ac  -= np.polyval(ir_slope,  t)
    red_ac -= np.polyval(red_slope, t)

    # ── 2. HR via autocorrelation ───────────────────────────────────────────
    #    Search lag range: 0.4 s–1.5 s → 40–150 BPM at 100 Hz
    #    (lag_min = 100/150*60 = 40 samples → 150 BPM max)
    lag_min = int(round(sampling_freq * 60.0 / 150))  # 40 samples
    lag_max = int(round(sampling_freq * 60.0 / 40))   # 150 samples
    # Use 3/4 of buffer so lags up to 150 samples are reachable even with
    # n=200 (n//2=100 would block 40-60 BPM range and prevent harmonic check).
    lag_max = min(lag_max, n * 3 // 4)

    if lag_min >= lag_max:
        return -999.0, False, -999.0, False

    # Normalised autocorrelation
    autocorr = np.correlate(ir_ac, ir_ac, mode='full')
    autocorr = autocorr[n - 1:]          # keep lags 0, 1, 2, …
    autocorr /= (autocorr[0] + 1e-9)     # normalise to 1.0 at lag=0

    search   = autocorr[lag_min:lag_max + 1]
    best_idx = int(np.argmax(search))
    best_lag = best_idx + lag_min
    best_corr = float(search[best_idx])

    # ── Boundary + harmonic guard ─────────────────────────────────────────────
    #    If best_lag hit either search boundary the argmax found no real peak
    #    inside the window.  Require very high confidence before accepting:
    #      lag == lag_min → reported HR 150 BPM (virtually never true at rest)
    #      lag == lag_max → reported HR 40 BPM (extremely slow / touching edge)
    if (best_lag == lag_min or best_lag == lag_max) and best_corr < 0.55:
        return -999.0, False, -999.0, False

    doubled_lag = best_lag * 2
    if doubled_lag <= lag_max:
        doubled_corr = float(autocorr[doubled_lag])
        # For best_lag ≤ 65 (reported HR > 92 BPM): with only ~2 cycles in a
        # 200-sample buffer the autocorr at T can go slightly negative due to
        # noise, so the "> 0" guard incorrectly blocks the doubling.
        # A resting HR > 92 BPM sitting still is extremely unusual; the cause
        # is almost always a T/2 dicrotic-notch peak. Double unconditionally.
        # For normal HR range (best_lag > 65) keep the 0.10 guard.
        if best_lag <= 65 or doubled_corr >= 0.10 * best_corr:
            best_lag  = doubled_lag
            best_corr = doubled_corr

    _log.info(
        "MAX30102 autocorr: best_lag=%d  corr=%.3f  → %.1f BPM",
        best_lag, best_corr, (sampling_freq / best_lag) * 60.0,
    )

    # Require at least 0.25 correlation after detrending.
    # With linear detrend, stable PPG gives ~0.30–0.55; settling/noisy reads
    # sit at 0.10–0.24 and almost always produce wrong lags (120–150 range).
    if best_corr < 0.25:
        return -999.0, False, -999.0, False

    hr_bpm   = (sampling_freq / best_lag) * 60.0
    hr_valid = 40 <= hr_bpm <= 150

    # ── 3. SpO2 via DFT amplitude at cardiac frequency ─────────────────────
    #    We already know the cardiac period = best_lag samples.
    #    Evaluate the DFT at exactly that frequency bin — this extracts ONLY
    #    the pulsatile cardiac component and rejects everything else
    #    (dicrotic notch at 2×, motion at other frequencies, wideband noise).
    k = int(round(n / best_lag))   # DFT bin of cardiac fundamental
    if k < 1 or k >= n // 2:
        return hr_bpm, hr_valid, -999.0, False

    ir_fft  = np.fft.rfft(ir_ac)
    red_fft = np.fft.rfft(red_ac)

    ac_ir  = 2.0 * float(np.abs(ir_fft[k]))  / n
    ac_red = 2.0 * float(np.abs(red_fft[k])) / n

    # Minimum absolute AC signal gate.
    # When the DFT bin amplitude is tiny (<50 counts), spectral noise from
    # breathing or quantisation dominates R — producing R values of 0.1 or 4+.
    # Both channels must have a meaningful pulsatile component.
    if ac_ir < 50.0 or ac_red < 10.0:
        _log.debug(
            "MAX30102 SpO2 rejected: ac signals too small (ac_ir=%.1f, ac_red=%.1f)",
            ac_ir, ac_red,
        )
        return hr_bpm, hr_valid, -999.0, False

    # Perfusion index check: reject pure noise (AC/DC too low).
    # Normal PPG: 0.05%–15%. Pure noise: <0.01%.
    pi_ir = ac_ir / ir_mean
    if pi_ir < 0.0001:
        _log.warning(
            "MAX30102 SpO2 rejected: perfusion_index=%.4f%% (need >0.01%%)",
            pi_ir * 100,
        )
        return hr_bpm, hr_valid, -999.0, False

    r = (ac_red / red_mean) / (ac_ir / ir_mean)
    _log.info(
        "MAX30102 SpO2: ac_ir=%.1f  ac_red=%.1f  PI=%.3f%%  R=%.3f",
        ac_ir, ac_red, pi_ir * 100, r,
    )
    # R < 0.3 → SpO2 > 100% (calibration noise); R > 0.95 → SpO2 < 81%
    # which is non-physiological for a sensor read without severe hypoxia.
    # R ≥ 1.0 is physically impossible. Reject these as motion artifacts.
    if r < 0.3 or r > 0.95:
        _log.warning(
            "MAX30102 SpO2 rejected: R=%.3f out of physiological range [0.30, 0.95]",
            r,
        )
        return hr_bpm, hr_valid, -999.0, False
    r_idx    = max(0, min(int(r * 100), len(_SPO2_TABLE) - 1))
    spo2     = float(_SPO2_TABLE[r_idx])
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
