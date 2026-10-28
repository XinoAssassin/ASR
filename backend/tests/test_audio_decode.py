"""Probe / decode through PyAV and the imageio-ffmpeg fallback (asr.audio.decode)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from asr.audio import decode
from asr.audio.decode import AudioDecodeError, NoAudioStreamError
from asr.audio.formats import ACCEPTED_EXTENSIONS, is_accepted
from tests.test_audio_fixtures import (
    FORMATS,
    REAL_EXCERPT,
    correlated_stereo,
    write_audio,
    write_multistream,
)

EXPECTED_CODEC = {
    "wav_s16": "pcm_s16le",
    "wav_f32": "pcm_f32le",
    "flac": "flac",
    "mp3": "mp3",
    "m4a": "aac",
    "aac": "aac",
    "ogg": "opus",
    "opus": "opus",
    "webm": "opus",
    "mkv": "flac",
    "mov": "aac",
    "caf": "pcm_s16le",
    "wav_8k": "pcm_s16le",
}


@pytest.fixture(params=["pyav", "imageio"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ASR_DECODER", request.param)
    return str(request.param)


# ------------------------------------------------------------------------------------- formats
def test_accepted_extensions_match_recording_sources() -> None:
    assert (
        frozenset(
            [
                "m4a",
                "mp3",
                "wav",
                "flac",
                "aac",
                "caf",
                "ogg",
                "opus",
                "webm",
                "mp4",
                "m4v",
                "mov",
                "mkv",
            ]
        )
        == ACCEPTED_EXTENSIONS
    )


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("a.m4a", True),
        ("A.M4A", True),
        ("x.y.WebM", True),
        ("meeting.txt", False),
        ("m4a", False),
        (".m4a", False),
        ("noext", False),
        ("a.m4a.exe", False),
    ],
)
def test_is_accepted(name: str, ok: bool) -> None:
    assert is_accepted(name) is ok


# --------------------------------------------------------------------------------------- probe
@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_probe_fields_per_format(tmp_path: Path, kind: str, backend: str) -> None:
    channels = 1 if kind in ("wav_8k", "opus", "caf") else 2
    rate = FORMATS[kind][3]
    samples = correlated_stereo(2.0, rate) if channels == 2 else None
    path = write_audio(tmp_path, kind, channels=channels, seconds=2.0, samples=samples)
    info = decode.probe(path)
    assert info.codec == EXPECTED_CODEC[kind]
    assert info.sample_rate == FORMATS[kind][3]
    assert info.channels == channels
    assert info.stream_index == 0
    assert info.audio_streams == 1
    assert info.size == path.stat().st_size
    assert info.container
    assert info.duration is not None and info.duration == pytest.approx(2.0, abs=0.1)
    if channels == 1:
        assert info.channel_policy == "mono"
        assert info.channel_correlation is None
    else:
        assert info.channel_policy == "downmix"
        assert info.channel_correlation is not None and info.channel_correlation > 0.3


def test_probe_bit_rate(tmp_path: Path) -> None:
    info = decode.probe(write_audio(tmp_path, "wav_s16"))
    assert info.bit_rate == 16_000 * 16


def test_probe_multistream(tmp_path: Path, backend: str) -> None:
    path = write_multistream(tmp_path)
    first = decode.probe(path, stream_index=0)
    second = decode.probe(path, stream_index=1)
    assert first.audio_streams == second.audio_streams == 2
    assert (first.channels, first.channel_policy) == (1, "mono")
    assert (second.channels, second.channel_policy, second.stream_index) == (2, "downmix", 1)
    with pytest.raises(NoAudioStreamError):
        decode.probe(path, stream_index=2)


def test_probe_garbage_raises_value_error(tmp_path: Path, backend: str) -> None:
    path = tmp_path / "garbage.m4a"
    path.write_bytes(os.urandom(64 * 1024))
    with pytest.raises(AudioDecodeError) as excinfo:
        decode.probe(path)
    assert isinstance(excinfo.value, ValueError)
    assert str(tmp_path) not in str(excinfo.value)


def test_probe_empty_and_missing(tmp_path: Path, backend: str) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(AudioDecodeError):
        decode.probe(empty)
    with pytest.raises(AudioDecodeError):
        decode.probe(tmp_path / "missing.wav")


def test_probe_truncated_m4a(tmp_path: Path) -> None:
    path = write_audio(tmp_path, "m4a", seconds=2.0)
    truncated = tmp_path / "truncated.m4a"
    truncated.write_bytes(path.read_bytes()[:200])
    with pytest.raises(AudioDecodeError):
        decode.probe(truncated)


# -------------------------------------------------------------------------------------- decode
@pytest.mark.parametrize("kind", ["wav_s16", "wav_f32", "flac", "mp3", "m4a", "ogg", "wav_8k"])
def test_decode_pcm16k_rate_length_range(tmp_path: Path, kind: str, backend: str) -> None:
    path = write_audio(tmp_path, kind, channels=1, seconds=2.0)
    x = decode.decode_pcm16k(path)
    assert x.dtype == np.float32
    assert x.ndim == 1
    assert abs(x.shape[0] - 32_000) <= 0.05 * 32_000  # codec priming / padding
    assert float(np.abs(x).max()) <= 1.0
    assert 0.2 < float(np.abs(x).max()) < 0.4


def test_decode_8k_is_upsampled(tmp_path: Path, backend: str) -> None:
    rate = 8000
    t = np.arange(rate * 2) / rate
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)[None, :]
    path = write_audio(tmp_path, "wav_8k", samples=tone)
    x = decode.decode_pcm16k(path)
    assert x.shape[0] == 32_000
    spectrum = np.abs(np.fft.rfft(x[1000:-1000]))
    peak_hz = np.argmax(spectrum) * 16_000 / (x.shape[0] - 2000)
    assert peak_hz == pytest.approx(1000, abs=5)


def test_decode_channel_selection_and_downmix(tmp_path: Path, backend: str) -> None:
    rate = 48_000
    t = np.arange(rate) / rate
    left = (0.6 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    right = (0.2 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    path = write_audio(tmp_path, "wav_f32", samples=np.stack([left, right]))
    mid = slice(1000, -1000)
    assert float(np.abs(decode.decode_pcm16k(path, channel=0)[mid]).max()) == pytest.approx(
        0.6, abs=0.01
    )
    assert float(np.abs(decode.decode_pcm16k(path, channel=1)[mid]).max()) == pytest.approx(
        0.2, abs=0.01
    )
    # Plain mean, not FFmpeg's -3 dB-per-channel matrix (which would give 0.566).
    assert float(np.abs(decode.decode_pcm16k(path)[mid]).max()) == pytest.approx(0.4, abs=0.01)
    with pytest.raises(ValueError):
        decode.decode_pcm16k(path, channel=2)


def test_downmix_of_identical_loud_channels_does_not_clip(tmp_path: Path, backend: str) -> None:
    t = np.arange(48_000) / 48_000
    tone = (0.9 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    path = write_audio(tmp_path, "wav_f32", samples=np.stack([tone, tone]))
    x = decode.decode_pcm16k(path)
    assert float(np.abs(x).max()) == pytest.approx(0.9, abs=0.01)


def test_decode_clips_float_overs(tmp_path: Path) -> None:
    t = np.arange(16_000) / 16_000
    hot = (1.5 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)[None, :]
    path = tmp_path / "hot.wav"
    from tests.test_audio_fixtures import encode

    encode(path, [hot], 16_000, container="wav", codec="pcm_f32le")
    x = decode.decode_pcm16k(path)
    assert float(np.abs(x).max()) <= 1.0


def test_decode_multistream_index(tmp_path: Path, backend: str) -> None:
    path = write_multistream(tmp_path)
    tone = decode.decode_pcm16k(path, stream_index=0)
    speech = decode.decode_pcm16k(path, stream_index=1)
    spectrum = np.abs(np.fft.rfft(tone))
    assert np.argmax(spectrum) * 16_000 / tone.shape[0] == pytest.approx(440, abs=3)
    assert not np.allclose(tone[: speech.shape[0]], speech[: tone.shape[0]])


def test_decode_multichannel_window(tmp_path: Path, backend: str) -> None:
    path = write_audio(tmp_path, "flac", samples=correlated_stereo(3.0, 44_100))
    x = decode.decode_multichannel(path, rate=8000, max_seconds=1.0)
    assert x.shape == (2, 8000)


def test_backends_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_audio(tmp_path, "m4a", channels=2, seconds=2.0)
    monkeypatch.setenv("ASR_DECODER", "pyav")
    a = decode.decode_pcm16k(path)
    pa = decode.probe(path)
    monkeypatch.setenv("ASR_DECODER", "imageio")
    b = decode.decode_pcm16k(path)
    pb = decode.probe(path)
    assert abs(a.shape[0] - b.shape[0]) <= 2048
    n = min(a.shape[0], b.shape[0])
    assert float(np.sqrt(np.mean(a[:n] ** 2))) == pytest.approx(
        float(np.sqrt(np.mean(b[:n] ** 2))), rel=0.05
    )
    assert (pa.codec, pa.sample_rate, pa.channels) == (pb.codec, pb.sample_rate, pb.channels)
    assert pa.channel_policy == pb.channel_policy


# ---------------------------------------------------------------------------- bundled binaries
def test_ffmpeg_version_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_DECODER", "pyav")
    assert "PyAV" in decode.ffmpeg_version()
    monkeypatch.setenv("ASR_DECODER", "imageio")
    assert "imageio-ffmpeg" in decode.ffmpeg_version()


def test_bundled_ffmpeg_is_inside_wheel() -> None:
    exe = Path(decode.bundled_ffmpeg_exe())
    assert exe.is_absolute()
    assert "imageio_ffmpeg" in exe.parts


def test_host_ffmpeg_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import imageio_ffmpeg

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    with pytest.raises(RuntimeError):
        decode.bundled_ffmpeg_exe()
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: "/usr/bin/ffmpeg")
    with pytest.raises(RuntimeError):
        decode.bundled_ffmpeg_exe()


# ----------------------------------------------------------------------------- real recording
@pytest.mark.skipif(not REAL_EXCERPT.exists(), reason="run scripts/make_fixtures.py first")
def test_real_excerpt_probe_and_decode(backend: str) -> None:
    info = decode.probe(REAL_EXCERPT)
    assert info.codec == "aac"
    assert info.channels == 2
    assert info.duration == pytest.approx(20.0, abs=0.2)
    assert info.channel_policy == "downmix"
    x = decode.decode_pcm16k(REAL_EXCERPT)
    assert x.shape[0] == pytest.approx(20 * 16_000, rel=0.01)
    assert float(np.abs(x).max()) > 0.01
