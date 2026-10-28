"""Channel-policy heuristic and resolution (asr.audio.channels)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from asr.audio import channels as ch
from asr.audio.channels import classify_stereo, detect_channel_policy, resolve_channels
from asr.audio.types import ChannelPolicy, ProbeInfo
from asr.providers.base import ChannelsOption
from tests.test_audio_fixtures import (
    REAL_EXCERPT,
    correlated_stereo,
    independent_stereo,
    speech_like,
    turns,
    write_audio,
)

RATE = 8000


# --------------------------------------------------------------------------- classify (arrays)
def test_identical_channels() -> None:
    s = speech_like(20, RATE)
    assert classify_stereo(s, s, RATE) == ("downmix", 1.0)


def test_correlated_mics_with_delay_reverb_noise() -> None:
    x = correlated_stereo(20, RATE)
    policy, corr = classify_stereo(x[0], x[1], RATE)
    assert policy == "downmix"
    assert corr is not None and corr >= ch.CORRELATED_MIN


def test_widely_spaced_mics_low_correlation_but_same_levels() -> None:
    rng = np.random.default_rng(7)
    s = speech_like(20, RATE).astype(np.float64)
    irs = []
    for _ in range(2):
        ir = rng.standard_normal(int(0.3 * RATE)) * np.exp(-np.arange(int(0.3 * RATE)) / 600)
        ir[0] = 2.0
        irs.append(ir)
    left = np.convolve(s, irs[0])[: s.shape[0]]
    right = np.convolve(s, irs[1])[: s.shape[0]]
    policy, _ = classify_stereo(left, right, RATE)
    assert policy == "downmix"


def test_independent_alternating_talkers() -> None:
    x = independent_stereo(30, RATE)
    policy, corr = classify_stereo(x[0], x[1], RATE)
    assert policy == "per_channel"
    assert corr is not None and abs(corr) < 0.1


def test_independent_with_bleed_and_noise() -> None:
    rng = np.random.default_rng(1)
    x = independent_stereo(30, RATE).astype(np.float64)
    n = x.shape[1]
    left = x[0] + 0.15 * np.roll(x[1], 40) + 0.002 * rng.standard_normal(n)
    right = x[1] + 0.002 * rng.standard_normal(n)
    assert classify_stereo(left, right, RATE)[0] == "per_channel"


def test_unbalanced_call_one_party_talks_most() -> None:
    # Left talks 9 s of every 18 s, right answers for 2 s in the gap.
    n = 60 * RATE
    cycle = (np.arange(n) / RATE) % 18
    left = speech_like(60, RATE, seed=4) * turns(n, RATE, 9.0, 0)
    right = speech_like(60, RATE, seed=5) * ((cycle >= 10) & (cycle < 12))
    assert classify_stereo(left, right, RATE)[0] == "per_channel"


@pytest.mark.parametrize(
    "right",
    [
        np.zeros(20 * RATE, dtype=np.float32),
        1e-5 * np.random.default_rng(0).standard_normal(20 * RATE),
        0.005 * np.random.default_rng(1).standard_normal(20 * RATE),  # steady noise only
    ],
    ids=["digital-silence", "near-silence", "noise-only"],
)
def test_silent_or_noise_only_channel_downmixes(right: np.ndarray) -> None:
    left = speech_like(20, RATE)
    assert classify_stereo(left, right, RATE)[0] == "downmix"


def test_phase_inverted_reports_negative_correlation() -> None:
    s = speech_like(10, RATE)
    policy, corr = classify_stereo(s, -s, RATE)
    assert policy == "downmix"
    assert corr == pytest.approx(-1.0, abs=1e-3)


def test_too_short_is_not_analysed() -> None:
    s = speech_like(0.5, RATE)
    assert classify_stereo(s, s, RATE) == ("downmix", None)


# ------------------------------------------------------------------------------ detect (files)
def test_detect_mono_file(tmp_path: Path) -> None:
    assert detect_channel_policy(write_audio(tmp_path, "wav_s16")) == ("mono", None)


@pytest.mark.parametrize("kind", ["wav_f32", "flac", "m4a"])
def test_detect_correlated_file(tmp_path: Path, kind: str) -> None:
    from tests.test_audio_fixtures import FORMATS

    rate = FORMATS[kind][3]
    path = write_audio(tmp_path, kind, samples=correlated_stereo(10, rate))
    policy, corr = detect_channel_policy(path)
    assert policy == "downmix"
    assert corr is not None and corr > 0.5


@pytest.mark.parametrize("kind", ["wav_f32", "m4a"])
def test_detect_independent_file(tmp_path: Path, kind: str) -> None:
    from tests.test_audio_fixtures import FORMATS

    rate = FORMATS[kind][3]
    path = write_audio(tmp_path, kind, samples=independent_stereo(20, rate))
    assert detect_channel_policy(path)[0] == "per_channel"


def test_detect_silent_right_file(tmp_path: Path) -> None:
    rate = 48_000
    left = speech_like(10, rate)
    path = write_audio(tmp_path, "wav_f32", samples=np.stack([left, np.zeros_like(left)]))
    assert detect_channel_policy(path)[0] == "downmix"


def test_detect_more_than_two_channels(tmp_path: Path) -> None:
    rate = 48_000
    samples = np.stack([speech_like(3, rate, seed=i) for i in range(4)])
    path = write_audio(tmp_path, "wav_f32", samples=samples)
    assert detect_channel_policy(path) == ("downmix", None)


def test_detect_window_limits_decoding(tmp_path: Path) -> None:
    # Correlated first 5 s, independent afterwards: a 4 s window only sees the first part.
    rate = 16_000
    head = correlated_stereo(5, rate)
    tail = independent_stereo(30, rate)
    path = write_audio(tmp_path, "wav_s16", samples=np.concatenate([head, tail], axis=1))
    assert detect_channel_policy(path, window_s=4.0)[0] == "downmix"
    assert detect_channel_policy(path, window_s=120.0)[0] == "per_channel"


@pytest.mark.skipif(not REAL_EXCERPT.exists(), reason="run scripts/make_fixtures.py first")
def test_real_excerpt_is_correlated() -> None:
    policy, corr = detect_channel_policy(REAL_EXCERPT)
    assert policy == "downmix"
    assert corr is not None and corr > 0.9


# ------------------------------------------------------------------------------------- resolve
def _probe(channels: int, policy: ChannelPolicy) -> ProbeInfo:
    return ProbeInfo(
        container="wav",
        codec="pcm_s16le",
        duration=1.0,
        sample_rate=16_000,
        channels=channels,
        bit_rate=None,
        stream_index=0,
        audio_streams=1,
        size=1,
        channel_policy=policy,
        channel_correlation=None,
    )


@pytest.mark.parametrize(
    ("channels", "policy", "option", "mode", "ids"),
    [
        (1, "mono", "auto", "mono", [0]),
        (2, "downmix", "auto", "downmix", [0]),
        (2, "per_channel", "auto", "per_channel", [0, 1]),
        (4, "downmix", "auto", "downmix", [0]),
        (1, "mono", "downmix", "mono", [0]),
        (2, "per_channel", "downmix", "downmix", [0]),
        (1, "mono", "left", "select", [0]),
        (2, "downmix", "left", "select", [0]),
        (2, "downmix", "right", "select", [1]),
        (2, "downmix", "per_channel", "per_channel", [0, 1]),
        (3, "downmix", "per_channel", "per_channel", [0, 1, 2]),
    ],
)
def test_resolve_channels_table(
    channels: int, policy: ChannelPolicy, option: ChannelsOption, mode: str, ids: list[int]
) -> None:
    resolved = resolve_channels(_probe(channels, policy), option)
    assert resolved.mode == mode
    assert resolved.channel_ids == ids


@pytest.mark.parametrize("option", ["right", "per_channel"])
def test_resolve_channels_rejects_on_mono(option: ChannelsOption) -> None:
    with pytest.raises(ValueError):
        resolve_channels(_probe(1, "mono"), option)
