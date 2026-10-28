"""Default-on preprocessing steps (PLAN §4.5 [0d], [0e]). Owned by ``backend-core`` (M1).

Decode-only by default: nothing non-linear runs unless ``PreprocessOptions`` enables it.
"""

import numpy as np


def peak_safety_gain(x: np.ndarray) -> tuple[np.ndarray, float]:
    """If peak < -20 dBFS, apply linear gain to -3 dBFS. Returns ``(signal, gain)``."""
    raise NotImplementedError


def clipping_ratio(x: np.ndarray) -> float:
    """Fraction of samples at or near full scale (warning only)."""
    raise NotImplementedError
