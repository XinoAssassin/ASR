"""Synthetic audio for the audio / upload tests, written with PyAV (EXECUTION_LOG D10).

Imported by the other ``test_audio_*`` and ``test_routes_*`` modules; the tests at the bottom
check that the generators themselves behave.
"""

from __future__ import annotations

import io
from pathlib import Path

import av
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_EXCERPT = REPO_ROOT / "tests" / "fixtures" / "4people_20s.m4a"

# name -> (container format, encoder, extension, sample rate)
FORMATS: dict[str, tuple[str, str, str, int]] = {
    "wav_s16": ("wav", "pcm_s16le", "wav", 16_000),
    "wav_f32": ("wav", "pcm_f32le", "wav", 48_000),
    "flac": ("flac", "flac", "flac", 44_100),
    "mp3": ("mp3", "libmp3lame", "mp3", 44_100),
    "m4a": ("ipod", "aac", "m4a", 48_000),
    "aac": ("adts", "aac", "aac", 44_100),
    "ogg": ("ogg", "libopus", "ogg", 48_000),
    "opus": ("opus", "libopus", "opus", 48_000),
    "webm": ("webm", "libopus", "webm", 48_000),
    "mkv": ("matroska", "flac", "mkv", 48_000),
    "mov": ("mov", "aac", "mov", 44_100),
    "caf": ("caf", "pcm_s16le", "caf", 16_000),
    "wav_8k": ("wav", "pcm_s16le", "wav", 8_000),
}


def speech_like(seconds: float, rate: int, seed: int = 0, level: float = 0.3) -> np.ndarray:
    """Voiced-speech stand-in: harmonic series with a gliding f0, noise, 4 Hz syllable envelope."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    f0 = 110 + 25 * seed % 60 + 30 * np.sin(2 * np.pi * 0.3 * t + seed)
    phase = 2 * np.pi * np.cumsum(f0) / rate
    x = sum((1.0 / k) * np.sin(k * phase) for k in range(1, 16) if k * f0.max() < rate / 2)
    x = x + 0.3 * rng.standard_normal(n)
    envelope = (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t + rng.uniform(0, 6))) ** 2
    x = x * envelope
    return (level * x / np.max(np.abs(x))).astype(np.float32)


def turns(n: int, rate: int, period_s: float, phase: int) -> np.ndarray:
    """0/1 gate that is open every other ``period_s`` window (``phase`` 0 or 1)."""
    t = np.arange(n) / rate
    return (((t // period_s) % 2) == phase).astype(np.float32)


def correlated_stereo(seconds: float, rate: int, seed: int = 0) -> np.ndarray:
    """Two mics of one device: same source, 0.4 ms inter-mic delay, short reverb, mic noise."""
    rng = np.random.default_rng(seed + 100)
    src = speech_like(seconds, rate, seed)
    n = src.shape[0]
    delay = max(1, int(0.0004 * rate))
    ir_len = int(0.05 * rate)
    ir = rng.standard_normal(ir_len) * np.exp(-np.arange(ir_len) / (0.01 * rate))
    ir[0] = 6.0
    ir /= np.abs(ir).sum() / 2
    right = np.convolve(np.roll(src, delay), ir)[:n] * 0.8 + 0.002 * rng.standard_normal(n)
    return np.stack([src, right.astype(np.float32)])


def independent_stereo(seconds: float, rate: int) -> np.ndarray:
    """Telephony / OBS mic-L + remote-R: different talkers alternating per channel."""
    n = int(seconds * rate)
    left = speech_like(seconds, rate, seed=2) * turns(n, rate, 1.7, 0)
    right = speech_like(seconds, rate, seed=3) * turns(n, rate, 1.7, 1)
    return np.stack([left, right]).astype(np.float32)


def layout_name(channels: int) -> str:
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels} channels")


def encode(
    target: Path | io.BytesIO,
    streams: list[np.ndarray],
    rate: int,
    *,
    container: str,
    codec: str,
) -> None:
    """Encode one or more ``(channels, n)`` float32 arrays as audio streams of one container."""
    with av.open(target, mode="w", format=container) as out:
        encoders = [
            out.add_stream(codec, rate=rate, layout=layout_name(samples.shape[0]))
            for samples in streams
        ]
        for stream, samples in zip(encoders, streams, strict=True):
            layout = layout_name(samples.shape[0])
            for start in range(0, samples.shape[1], 4096):
                block = np.ascontiguousarray(samples[:, start : start + 4096], dtype=np.float32)
                frame = av.AudioFrame.from_ndarray(block, format="fltp", layout=layout)
                frame.rate = rate
                frame.pts = start
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)


def write_audio(
    directory: Path,
    kind: str,
    *,
    channels: int = 1,
    seconds: float = 2.0,
    samples: np.ndarray | None = None,
    name: str | None = None,
) -> Path:
    """Write a test file of one of ``FORMATS``; default content is ``speech_like`` per channel."""
    container, codec, ext, rate = FORMATS[kind]
    if samples is None:
        samples = np.stack([speech_like(seconds, rate, seed=c) for c in range(channels)])
    path = directory / (name or f"{kind}_{samples.shape[0]}ch.{ext}")
    encode(path, [samples], rate, container=container, codec=codec)
    return path


def wav_bytes(seconds: float = 1.0, rate: int = 16_000, channels: int = 1) -> bytes:
    """In-memory 16-bit WAV for upload tests."""
    samples = np.stack([speech_like(seconds, rate, seed=c) for c in range(channels)])
    buf = io.BytesIO()
    encode(buf, [samples], rate, container="wav", codec="pcm_s16le")
    return buf.getvalue()


def write_multistream(directory: Path) -> Path:
    """MKV with two audio streams: mono 440 Hz tone, then stereo speech-like audio."""
    rate = 48_000
    t = np.arange(rate * 2) / rate
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[None, :]
    stereo = correlated_stereo(2.0, rate)
    path = directory / "multistream.mkv"
    encode(path, [tone, stereo], rate, container="matroska", codec="flac")
    return path


# --------------------------------------------------------------------------- generator checks
def test_write_audio_every_format(tmp_path: Path) -> None:
    for kind in FORMATS:
        path = write_audio(tmp_path, kind, channels=2 if kind != "wav_8k" else 1)
        assert path.stat().st_size > 0


def test_wav_bytes_is_riff() -> None:
    data = wav_bytes(0.5)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"


@pytest.mark.parametrize("seconds", [0.5, 3.0])
def test_speech_like_range(seconds: float) -> None:
    x = speech_like(seconds, 16_000)
    assert x.dtype == np.float32
    assert x.shape == (int(seconds * 16_000),)
    assert 0.29 < float(np.abs(x).max()) <= 0.3 + 1e-6
