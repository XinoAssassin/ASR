"""Default-on preprocessing steps (PLAN §4.5 [0d], [0e]). Owned by ``backend-core`` (M1).

Decode-only by default (AGENTS.md rule 1a): the only default transformation is a single linear
gain for very quiet recordings; nothing non-linear (compression, loudnorm, denoise) runs unless
``PreprocessOptions`` enables it.
"""

import numpy as np

QUIET_PEAK_DBFS = -20.0  # below this peak level the safety gain is applied ...
TARGET_PEAK_DBFS = -3.0  # ... bringing the peak to this level
MAX_GAIN_DB = 30.0  # never more gain than this (a -50 dBFS peak ends at -20 dBFS, not -3)
SILENCE_PEAK_DBFS = -60.0  # below this peak the signal is dither / noise floor: left alone
CLIP_LEVEL = 0.999  # |sample| at or above this counts as clipped (≈ -0.01 dBFS)


def peak_safety_gain(x: np.ndarray) -> tuple[np.ndarray, float]:
    """If the peak is below -20 dBFS, apply one linear gain so the peak sits at -3 dBFS.

    The gain is capped at ``MAX_GAIN_DB``, and a peak below ``SILENCE_PEAK_DBFS`` (dither,
    near-silence) is not amplified at all. Returns ``(signal, gain_db)``; ``gain_db`` is 0.0 when
    nothing was changed (loud enough, empty, near or digital silence). The input array is never
    modified.
    """
    if x.size == 0:
        return x, 0.0
    peak = float(np.max(np.abs(x)))
    if peak <= 0.0 or not np.isfinite(peak):
        return x, 0.0
    peak_db = 20.0 * np.log10(peak)
    if peak_db >= QUIET_PEAK_DBFS or peak_db < SILENCE_PEAK_DBFS:
        return x, 0.0
    gain_db = min(TARGET_PEAK_DBFS - peak_db, MAX_GAIN_DB)
    gain = np.float32(10.0 ** (gain_db / 20.0))
    y = (x.astype(np.float32, copy=False) * gain).astype(np.float32, copy=False)
    return y, round(float(gain_db), 3)


def clipping_ratio(x: np.ndarray) -> float:
    """Fraction of samples at or near full scale (warning only, no de-clipping)."""
    if x.size == 0:
        return 0.0
    return float(np.count_nonzero(np.abs(x) >= CLIP_LEVEL)) / float(x.size)
