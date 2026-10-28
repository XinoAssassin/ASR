"""Transcoding (asr.audio.transcode) and default preprocessing (asr.audio.preprocess)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from asr.audio import decode, transcode
from asr.audio.decode import AudioDecodeError
from asr.audio.preprocess import clipping_ratio, peak_safety_gain
from tests.test_audio_fixtures import REAL_EXCERPT, correlated_stereo, write_audio


@pytest.fixture(params=["pyav", "imageio"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ASR_DECODER", request.param)
    return str(request.param)


# ------------------------------------------------------------------------------------ transcode
@pytest.mark.parametrize(
    ("fmt", "rate", "channels", "expect_channels"),
    [
        ("flac", 16_000, 1, 1),
        ("flac", 16_000, None, 2),
        ("mp3", 22_050, None, 2),
        ("mp3", 16_000, 1, 1),
    ],
)
def test_transcode_outputs_probe_correctly(
    tmp_path: Path,
    backend: str,
    fmt: transcode.TranscodeFormat,
    rate: int,
    channels: int | None,
    expect_channels: int,
) -> None:
    src = write_audio(tmp_path, "m4a", samples=correlated_stereo(3.0, 48_000))
    dst = tmp_path / "out" / f"x.{fmt}"
    assert transcode.transcode_audio(src, dst, fmt=fmt, sample_rate=rate, channels=channels) == dst
    info = decode.probe(dst)
    assert info.codec == fmt
    assert info.sample_rate == rate
    assert info.channels == expect_channels
    assert info.duration == pytest.approx(3.0, abs=0.15)
    assert [p.name for p in dst.parent.iterdir()] == [dst.name]  # temp file renamed away


def test_transcode_flac_mono_is_channel_mean(tmp_path: Path, backend: str) -> None:
    t = np.arange(48_000) / 48_000
    tone = (0.8 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    src = write_audio(tmp_path, "wav_f32", samples=np.stack([tone, tone]))
    dst = transcode.transcode_audio(
        src, tmp_path / "m.flac", fmt="flac", sample_rate=16_000, channels=1
    )
    x = decode.decode_pcm16k(dst)
    assert float(np.abs(x[500:-500]).max()) == pytest.approx(0.8, abs=0.01)


def test_transcode_selects_stream(tmp_path: Path, backend: str) -> None:
    from tests.test_audio_fixtures import write_multistream

    src = write_multistream(tmp_path)
    dst = transcode.transcode_audio(
        src, tmp_path / "s1.flac", fmt="flac", sample_rate=16_000, channels=None, stream_index=1
    )
    assert decode.probe(dst).channels == 2


def test_transcode_mp3_bitrate(tmp_path: Path) -> None:
    src = write_audio(tmp_path, "wav_s16", seconds=4.0)
    dst = transcode.transcode_audio(
        src, tmp_path / "b.mp3", fmt="mp3", sample_rate=16_000, channels=None, bitrate=32_000
    )
    assert decode.probe(dst).bit_rate == 32_000


def test_transcode_invalid_parameters(tmp_path: Path) -> None:
    src = write_audio(tmp_path, "wav_s16")
    with pytest.raises(ValueError):
        transcode.transcode_audio(
            src, tmp_path / "a.mp3", fmt="mp3", sample_rate=96_000, channels=1
        )
    with pytest.raises(ValueError):
        transcode.transcode_audio(
            src, tmp_path / "a.flac", fmt="flac", sample_rate=16_000, channels=2
        )
    samples = np.stack([np.zeros(16_000, dtype=np.float32)] * 3)
    three = write_audio(tmp_path, "wav_s16", samples=samples, name="three.wav")
    with pytest.raises(ValueError):
        transcode.transcode_audio(
            three, tmp_path / "a.mp3", fmt="mp3", sample_rate=16_000, channels=None
        )


def test_transcode_garbage_leaves_no_partial_file(tmp_path: Path, backend: str) -> None:
    src = tmp_path / "bad.m4a"
    src.write_bytes(os.urandom(4096))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(AudioDecodeError):
        transcode.transcode_audio(src, out / "x.flac", fmt="flac", sample_rate=16_000, channels=1)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    ("rate", "channels", "expected"),
    [
        (48_000, 1, (48_000, None, 64_000)),
        (44_100, 2, (44_100, None, 96_000)),
        (96_000, 2, (48_000, None, 96_000)),
        (8_000, 1, (8_000, None, 64_000)),
        (37_800, 1, (44_100, None, 64_000)),
        (48_000, 6, (48_000, 1, 64_000)),
    ],
)
def test_browser_params(rate: int, channels: int, expected: tuple[int, int | None, int]) -> None:
    assert transcode.browser_params(rate, channels) == expected


def test_transcode_for_browser(tmp_path: Path, backend: str) -> None:
    src = write_audio(tmp_path, "wav_8k", seconds=2.0)
    dst = transcode.transcode_for_browser(src, tmp_path / "cache" / "x.mp3")
    info = decode.probe(dst)
    assert (info.codec, info.sample_rate, info.channels) == ("mp3", 8_000, 1)


@pytest.mark.skipif(not REAL_EXCERPT.exists(), reason="run scripts/make_fixtures.py first")
def test_real_excerpt_to_cloud_flac(tmp_path: Path) -> None:
    dst = transcode.transcode_audio(
        REAL_EXCERPT, tmp_path / "cloud.flac", fmt="flac", sample_rate=16_000, channels=1
    )
    info = decode.probe(dst)
    assert (info.sample_rate, info.channels) == (16_000, 1)
    assert info.duration == pytest.approx(20.0, abs=0.1)


# ---------------------------------------------------------------------------------- preprocess
def test_peak_gain_quiet_signal() -> None:
    x = (0.05 * np.sin(np.linspace(0, 100, 16_000))).astype(np.float32)  # -26 dBFS
    y, gain_db = peak_safety_gain(x)
    assert gain_db == pytest.approx(-3.0 - 20 * np.log10(0.05), abs=0.01)
    assert float(np.abs(y).max()) == pytest.approx(10 ** (-3 / 20), rel=1e-3)
    assert y.dtype == np.float32
    assert float(np.abs(x).max()) == pytest.approx(0.05, rel=1e-3)  # input untouched
    # Linear: the waveform shape is unchanged.
    np.testing.assert_allclose(y / y.max(), x / x.max(), rtol=1e-5, atol=1e-6)


@pytest.mark.parametrize("peak", [0.1, 0.5, 1.0])
def test_peak_gain_leaves_normal_levels(peak: float) -> None:
    x = (peak * np.sin(np.linspace(0, 100, 16_000))).astype(np.float32)
    y, gain_db = peak_safety_gain(x)
    assert gain_db == 0.0
    assert y is x


def test_peak_gain_silence_and_empty() -> None:
    zeros = np.zeros(100, dtype=np.float32)
    assert peak_safety_gain(zeros)[1] == 0.0
    assert peak_safety_gain(np.zeros(0, dtype=np.float32))[1] == 0.0


def test_clipping_ratio() -> None:
    x = np.zeros(1000, dtype=np.float32)
    assert clipping_ratio(x) == 0.0
    x[:10] = 1.0
    x[10:15] = -1.0
    x[15:20] = 0.99
    assert clipping_ratio(x) == pytest.approx(0.015)
    assert clipping_ratio(np.zeros(0, dtype=np.float32)) == 0.0
