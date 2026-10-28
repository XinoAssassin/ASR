"""Channel-policy heuristic and resolution (PLAN §4.5 [0b], docs/research/recording-sources.md).

``detect_channel_policy`` decides whether a 2-channel recording carries one acoustic scene (iPhone
multi-mic stereo, loopback stereo, mono-as-stereo → ``downmix``) or two independent parties
(telephony uplink/downlink, OBS mic-L + remote-R → ``per_channel``). When unsure it answers
``downmix``: averaging never loses speech, whereas recognising correlated channels separately
duplicates every utterance.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from asr.audio.types import ChannelPolicy, ProbeInfo, ResolvedChannels
from asr.providers.base import ChannelsOption

# Analysis parameters (see ``detect_channel_policy`` for how they are used).
ANALYSIS_RATE = 8_000  # Hz; speech band only, keeps the FFTs small
BAND_HZ = (200.0, 3_400.0)  # telephone band: drops hum / DC and the decorrelated top octave
MAX_LAG_S = 0.005  # ±5 ms covers inter-mic delays of one device (< 1 ms) and small offsets
FRAME_S = 0.05  # energy-envelope frame
MIN_ANALYSIS_S = 1.0  # shorter clips are not analysed (→ downmix)

SILENT_DBFS = -60.0  # channel RMS below this → silent
SILENT_REL_DB = 40.0  # channel RMS this far below the other channel → silent
ACTIVE_FLOOR_DBFS = -55.0  # envelope frame counts as active above this ...
ACTIVE_REL_DB = 30.0  # ... and within this many dB of the loud (95th percentile) frames

CORRELATED_MIN = 0.3  # |waveform correlation| at or above this → downmix
DOMINANCE_DB = 10.0  # frame "belongs" to one channel when it is this much louder
DOMINANT_FRACTION_MIN = 0.5  # per_channel needs most active frames dominated by one side ...
EACH_SIDE_FRACTION_MIN = 0.02  # ... and each side dominating at least this share
MODULATION_MIN_DB = 15.0  # speech envelope spread (p95 - p10); steady noise / hum stays below


def detect_channel_policy(
    path: Path,
    stream_index: int = 0,
    *,
    window_s: float = 120.0,
    channels: int | None = None,
) -> tuple[ChannelPolicy, float | None]:
    """Return the detected policy and, for 2-channel audio, the inter-channel correlation.

    - 1 channel → ``("mono", None)``; more than 2 channels → ``("downmix", None)``.
    - 2 channels: the first ``window_s`` seconds of both channels are decoded at 8 kHz and
      band-limited to 200–3400 Hz. Then:

      1. *Correlation*: the normalised cross-correlation ``Σ l·r / sqrt(Σl² Σr²)`` is evaluated
         for every lag within ±5 ms; the value with the largest magnitude is returned as
         ``channel_correlation`` (signed, in [-1, 1]).
      2. *Silence*: a channel whose RMS is below -60 dBFS, or more than 40 dB below the other
         channel, is silent → ``downmix``.
      3. ``|correlation| >= 0.3`` → ``downmix`` (same scene picked up by both channels:
         identical channels give ~1.0, two mics of one phone 0.5–0.95).
      4. *Dominance*: over 50 ms frames, a frame is active when the louder channel exceeds
         -55 dBFS and is within 30 dB of the loud (95th-percentile) frames. A frame is dominated
         by one side when that channel is ≥ 10 dB louder than the other. If ≥ 50 % of active
         frames are dominated by one side **and** each side dominates ≥ 2 % of active frames
         (two parties taking turns on separate channels), and both channels look like speech
         (frame-energy spread p95 − p10 ≥ 15 dB; a channel with only steady noise or hum fails)
         → ``per_channel``.
      5. Otherwise (weakly correlated but similar levels, e.g. widely spaced mics) → ``downmix``.

    Clips shorter than 1 s are not analysed (``downmix``). Strongly negative correlation
    (phase-inverted channels) still yields ``downmix``; the user can override with ``left`` /
    ``right``. ``channels`` may be passed when the caller already probed the stream.
    """
    from asr.audio import decode

    if channels is None:
        channels = decode.stream_meta(path, stream_index).channels
    if channels <= 1:
        return "mono", None
    if channels > 2:
        return "downmix", None

    x = decode.decode_multichannel(
        path, rate=ANALYSIS_RATE, stream_index=stream_index, max_seconds=window_s
    )
    if x.ndim != 2 or x.shape[0] != 2:
        return "downmix", None
    return classify_stereo(x[0], x[1], ANALYSIS_RATE)


def classify_stereo(
    left: np.ndarray, right: np.ndarray, rate: int
) -> tuple[ChannelPolicy, float | None]:
    """Apply the 2-channel heuristic of ``detect_channel_policy`` to decoded samples."""
    n = min(left.shape[0], right.shape[0])
    if n < int(MIN_ANALYSIS_S * rate):
        return "downmix", None
    left = np.asarray(left[:n], dtype=np.float64)
    right = np.asarray(right[:n], dtype=np.float64)

    lf, rf, corr = _bandlimited_xcorr(left, right, rate)

    rms_l = _db(np.sqrt(np.mean(lf * lf)))
    rms_r = _db(np.sqrt(np.mean(rf * rf)))
    if min(rms_l, rms_r) < SILENT_DBFS or abs(rms_l - rms_r) > SILENT_REL_DB:
        return "downmix", corr
    if abs(corr) >= CORRELATED_MIN:
        return "downmix", corr

    frame = max(1, int(FRAME_S * rate))
    frames = n // frame
    if frames < 4:
        return "downmix", corr
    env_l = _db(np.sqrt(np.mean(lf[: frames * frame].reshape(frames, frame) ** 2, axis=1)))
    env_r = _db(np.sqrt(np.mean(rf[: frames * frame].reshape(frames, frame) ** 2, axis=1)))
    loud = np.maximum(env_l, env_r)
    threshold = max(ACTIVE_FLOOR_DBFS, float(np.percentile(loud, 95)) - ACTIVE_REL_DB)
    active = loud >= threshold
    if not active.any():
        return "downmix", corr
    diff = (env_l - env_r)[active]
    left_dominant = float(np.mean(diff >= DOMINANCE_DB))
    right_dominant = float(np.mean(diff <= -DOMINANCE_DB))
    modulation = min(_spread_db(env_l), _spread_db(env_r))
    if (
        left_dominant + right_dominant >= DOMINANT_FRACTION_MIN
        and min(left_dominant, right_dominant) >= EACH_SIDE_FRACTION_MIN
        and modulation >= MODULATION_MIN_DB
    ):
        return "per_channel", corr
    return "downmix", corr


def _spread_db(env_db: np.ndarray) -> float:
    return float(np.percentile(env_db, 95) - np.percentile(env_db, 10))


def _db(x: np.ndarray | float) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(x, 1e-10))


def _bandlimited_xcorr(
    left: np.ndarray, right: np.ndarray, rate: int
) -> tuple[np.ndarray, np.ndarray, float]:
    """Band-limit both channels and return them with the peak normalised cross-correlation."""
    n = left.shape[0]
    max_lag = round(MAX_LAG_S * rate)
    nfft = 1 << int(np.ceil(np.log2(n + max_lag + 1)))
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    mask = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    spec_l = np.fft.rfft(left, nfft) * mask
    spec_r = np.fft.rfft(right, nfft) * mask
    lf = np.fft.irfft(spec_l, nfft)[:n]
    rf = np.fft.irfft(spec_r, nfft)[:n]
    energy = float(np.sqrt(np.sum(lf * lf) * np.sum(rf * rf)))
    if energy <= 1e-12:
        return lf, rf, 0.0
    # Circular correlation without wrap-around thanks to the zero padding: index k is
    # Σ l[t + k] r[t]; negative lags sit at the end of the array.
    cc = np.fft.irfft(spec_l * np.conj(spec_r), nfft)
    lags = np.concatenate([cc[: max_lag + 1], cc[-max_lag:]]) if max_lag else cc[:1]
    peak = lags[int(np.argmax(np.abs(lags)))] / energy
    return lf, rf, round(float(np.clip(peak, -1.0, 1.0)), 4)


def resolve_channels(probe: ProbeInfo, option: ChannelsOption) -> ResolvedChannels:
    """Combine probed metadata with the job's channel option.

    | option        | 1 channel     | 2+ channels                                  |
    |---------------|---------------|----------------------------------------------|
    | ``auto``      | mono [0]      | from ``probe.channel_policy``                |
    | ``downmix``   | mono [0]      | downmix [0]                                  |
    | ``left``      | select [0]    | select [0]                                   |
    | ``right``     | ``ValueError``| select [1]                                   |
    | ``per_channel``| ``ValueError``| per_channel [0 .. n-1]                      |
    """
    n = probe.channels
    if n < 1:
        raise ValueError("audio has no channels")
    if option == "auto":
        if n == 1 or probe.channel_policy == "mono":
            return ResolvedChannels(mode="mono", channel_ids=[0])
        if probe.channel_policy == "per_channel":
            return ResolvedChannels(mode="per_channel", channel_ids=list(range(n)))
        return ResolvedChannels(mode="downmix", channel_ids=[0])
    if option == "downmix":
        mode = "mono" if n == 1 else "downmix"
        return ResolvedChannels(mode=mode, channel_ids=[0])
    if option == "left":
        return ResolvedChannels(mode="select", channel_ids=[0])
    if option == "right":
        if n < 2:
            raise ValueError("channels=right needs a stereo file; this one is mono")
        return ResolvedChannels(mode="select", channel_ids=[1])
    if option == "per_channel":
        if n < 2:
            raise ValueError("channels=per_channel needs a multi-channel file; this one is mono")
        return ResolvedChannels(mode="per_channel", channel_ids=list(range(n)))
    raise ValueError(f"unknown channels option: {option!r}")
