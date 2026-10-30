"""Probe and decode via the bundled FFmpeg (docs/research/bundled-ffmpeg.md).

Two backends, selected per call by the ``ASR_DECODER`` environment variable:

- ``pyav`` (default): in-process decoding through PyAV's bundled FFmpeg libraries.
- ``imageio``: the static ``ffmpeg`` binary shipped inside the ``imageio-ffmpeg`` wheel, driven via
  ``subprocess`` with list arguments (no shell). The binary is required to live inside the wheel;
  a host ``ffmpeg`` from ``PATH`` is never used (AGENTS.md rule 1).

Every function here is blocking CPU / IO work; async callers use ``run_in_threadpool``.

Downmixing is done in NumPy as the plain channel mean. FFmpeg's own float downmix applies
-3 dB per channel without normalisation (two identical channels at 0.9 become 1.27), so it is
avoided on purpose.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Literal

import av
import numpy as np
from av.audio.resampler import AudioResampler

from asr.audio.types import ProbeInfo

TARGET_RATE = 16_000

DecoderBackend = Literal["pyav", "imageio"]

# Bytes read per ``stdout.read`` from the imageio subprocess (multiple of 4 * 8 channels).
_PIPE_BLOCK = 1 << 18


class AudioDecodeError(ValueError):
    """The file cannot be opened or decoded as audio (corrupt, unsupported, truncated)."""


class NoAudioStreamError(AudioDecodeError):
    """The container has no audio stream, or ``stream_index`` is out of range."""


@dataclass(frozen=True, slots=True)
class StreamMeta:
    """Metadata of one audio stream, without the channel-policy analysis."""

    container: str
    codec: str
    duration: float | None
    sample_rate: int
    channels: int
    bit_rate: int | None
    stream_index: int
    audio_streams: int
    size: int


def decoder_backend() -> DecoderBackend:
    """Backend chosen by ``ASR_DECODER`` (``pyav`` unless set to ``imageio``)."""
    value = os.environ.get("ASR_DECODER", "pyav").strip().lower()
    return "imageio" if value == "imageio" else "pyav"


# ------------------------------------------------------------------------------------ public API
def probe(path: Path, stream_index: int = 0) -> ProbeInfo:
    """Container / stream metadata for the ``stream_index``-th audio stream.

    Also runs the channel-correlation heuristic (``channels.detect_channel_policy``), which
    decodes up to two minutes of a 2-channel stream. Raises ``AudioDecodeError`` (a
    ``ValueError``) when the file is not decodable audio and ``NoAudioStreamError`` when the
    requested audio stream does not exist.
    """
    from asr.audio.channels import detect_channel_policy  # circular: channels uses decode

    meta = stream_meta(path, stream_index)
    policy, correlation = detect_channel_policy(path, stream_index, channels=meta.channels)
    return ProbeInfo(
        container=meta.container,
        codec=meta.codec,
        duration=meta.duration,
        sample_rate=meta.sample_rate,
        channels=meta.channels,
        bit_rate=meta.bit_rate,
        stream_index=meta.stream_index,
        audio_streams=meta.audio_streams,
        size=meta.size,
        channel_policy=policy,
        channel_correlation=correlation,
    )


def stream_meta(path: Path, stream_index: int = 0) -> StreamMeta:
    """Metadata of one audio stream; verifies that its first frame decodes."""
    path = Path(path)
    if stream_index < 0:
        raise NoAudioStreamError(f"stream_index must be >= 0, got {stream_index}")
    if not path.is_file():
        raise AudioDecodeError(f"not a file: {path.name}")
    if decoder_backend() == "imageio":
        return _meta_imageio(path, stream_index)
    return _meta_pyav(path, stream_index)


def decode_pcm16k(path: Path, *, channel: int | None = None, stream_index: int = 0) -> np.ndarray:
    """Decode to float32 mono 16 kHz in [-1, 1].

    ``channel=None`` averages all channels; ``channel=k`` keeps input channel ``k`` only
    (``ValueError`` if the stream has fewer channels). 8 kHz sources are upsampled in the same
    pass.
    """
    if channel is not None and channel < 0:
        raise ValueError(f"channel must be >= 0, got {channel}")
    parts: list[np.ndarray] = []
    for block in iter_decoded(path, rate=TARGET_RATE, stream_index=stream_index):
        if channel is None:
            parts.append(block.mean(axis=0, dtype=np.float32))
        else:
            if channel >= block.shape[0]:
                raise ValueError(f"channel {channel} requested but stream has {block.shape[0]}")
            parts.append(block[channel])
    if not parts:
        return np.zeros(0, dtype=np.float32)
    out = np.concatenate(parts).astype(np.float32, copy=False)
    np.clip(out, -1.0, 1.0, out=out)
    return out


def decode_multichannel(
    path: Path, *, rate: int, stream_index: int = 0, max_seconds: float | None = None
) -> np.ndarray:
    """Decode keeping every channel: float32 array of shape ``(channels, samples)`` at ``rate``."""
    blocks = list(iter_decoded(path, rate=rate, stream_index=stream_index, max_seconds=max_seconds))
    if not blocks:
        return np.zeros((0, 0), dtype=np.float32)
    if len({b.shape[0] for b in blocks}) > 1:
        raise AudioDecodeError(f"channel count changed while decoding {Path(path).name}")
    out = np.concatenate(blocks, axis=1).astype(np.float32, copy=False)
    if max_seconds is not None:
        out = out[:, : round(max_seconds * rate)]
    return out


def iter_decoded(
    path: Path, *, rate: int, stream_index: int = 0, max_seconds: float | None = None
) -> Iterator[np.ndarray]:
    """Yield float32 blocks of shape ``(channels, n)`` resampled to ``rate``, channels kept."""
    path = Path(path)
    if decoder_backend() == "imageio":
        yield from _iter_imageio(
            path, rate=rate, stream_index=stream_index, max_seconds=max_seconds
        )
    else:
        yield from _iter_pyav(path, rate=rate, stream_index=stream_index, max_seconds=max_seconds)


def ffmpeg_version() -> str:
    """Human-readable version of the bundled FFmpeg used by the active backend."""
    if decoder_backend() == "imageio":
        return _imageio_version()
    return f"FFmpeg {av.ffmpeg_version_info} (PyAV {av.__version__})"


# ------------------------------------------------------------------------------------------ PyAV
def _open_pyav(path: Path) -> av.container.InputContainer:
    try:
        return av.open(str(path), mode="r")
    except av.FFmpegError as exc:
        raise AudioDecodeError(f"cannot open {path.name}: {_ffmpeg_reason(exc)}") from exc


def _ffmpeg_reason(exc: Exception) -> str:
    # FFmpegError's str() repeats the full path; keep only the reason text.
    strerror = getattr(exc, "strerror", None)
    return str(strerror) if strerror else type(exc).__name__


def _select_pyav_stream(
    container: av.container.InputContainer, stream_index: int
) -> av.audio.stream.AudioStream:
    streams = container.streams.audio
    if not streams:
        raise NoAudioStreamError("no audio stream")
    if stream_index >= len(streams):
        raise NoAudioStreamError(
            f"audio stream {stream_index} requested but the file has {len(streams)}"
        )
    return streams[stream_index]


def _meta_pyav(path: Path, stream_index: int) -> StreamMeta:
    size = path.stat().st_size
    with _open_pyav(path) as container:
        stream = _select_pyav_stream(container, stream_index)
        ctx = stream.codec_context
        try:
            codec = ctx.codec.canonical_name or ctx.name
        except Exception:  # pragma: no cover - codec descriptor missing
            codec = ctx.name or "unknown"
        sample_rate = int(stream.rate or ctx.sample_rate or 0)
        channels = int(stream.layout.nb_channels) if stream.layout else 0

        duration: float | None = None
        if stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        elif container.duration:
            duration = container.duration / av.time_base
        bit_rate = stream.bit_rate or (container.bit_rate if len(container.streams) == 1 else None)

        # Opening succeeds for some damaged files; require one decodable frame.
        first = None
        try:
            for frame in container.decode(stream):
                first = frame
                break
        except av.FFmpegError as exc:
            raise AudioDecodeError(f"cannot decode {path.name}: {_ffmpeg_reason(exc)}") from exc
        if first is None:
            raise AudioDecodeError(f"no decodable audio in {path.name}")
        sample_rate = sample_rate or int(first.sample_rate)
        channels = channels or int(first.layout.nb_channels)
        container_name = container.format.name
        audio_streams = len(container.streams.audio)

    if duration is None:
        duration = _packet_duration_pyav(path, stream_index)
    if sample_rate <= 0 or channels <= 0:
        raise AudioDecodeError(f"invalid audio parameters in {path.name}")
    return StreamMeta(
        container=container_name,
        codec=codec,
        duration=round(duration, 6) if duration is not None else None,
        sample_rate=sample_rate,
        channels=channels,
        bit_rate=int(bit_rate) if bit_rate else None,
        stream_index=stream_index,
        audio_streams=audio_streams,
        size=size,
    )


def _packet_duration_pyav(path: Path, stream_index: int) -> float | None:
    """Duration from the last packet end when the container does not state one (demux only)."""
    end = 0.0
    try:
        with _open_pyav(path) as container:
            stream = _select_pyav_stream(container, stream_index)
            tb = stream.time_base
            if tb is None:
                return None
            start = float(stream.start_time * tb) if stream.start_time is not None else 0.0
            for packet in container.demux(stream):
                if packet.pts is None:
                    continue
                end = max(end, float((packet.pts + (packet.duration or 0)) * tb))
    except (av.FFmpegError, AudioDecodeError):
        return None
    return max(end - start, 0.0) if end > 0 else None


def _iter_pyav(
    path: Path, *, rate: int, stream_index: int, max_seconds: float | None
) -> Iterator[np.ndarray]:
    limit = round(max_seconds * rate) if max_seconds is not None else None
    produced = 0
    with _open_pyav(path) as container:
        stream = _select_pyav_stream(container, stream_index)
        stream.thread_type = "AUTO"
        # The output layout is pinned to the first frame's, so a mid-stream layout change
        # (concatenated mono + stereo ADTS / MP3) is remixed instead of changing the block shape.
        # PyAV's resampler turns into a passthrough when its first input already matches the
        # output and would then pass later frames through unchanged, so an input change always
        # starts a new resampler.
        resampler: AudioResampler | None = None
        layout = None
        current = None
        try:
            for frame in container.decode(stream):
                key = (frame.format.name, frame.layout.name, frame.sample_rate)
                if resampler is None:
                    layout = frame.layout.name
                elif key != current:
                    yield from (f.to_ndarray() for f in resampler.resample(None))
                    resampler = None
                if resampler is None:
                    resampler = AudioResampler(format="fltp", layout=layout, rate=rate)
                    current = key
                out = resampler.resample(frame)
                for f in out:
                    block = f.to_ndarray()
                    produced += block.shape[1]
                    yield block
                if limit is not None and produced >= limit:
                    return
            for f in resampler.resample(None) if resampler is not None else []:
                yield f.to_ndarray()
        except av.FFmpegError as exc:
            raise AudioDecodeError(f"cannot decode {path.name}: {_ffmpeg_reason(exc)}") from exc


# --------------------------------------------------------------------------------- imageio-ffmpeg
def bundled_ffmpeg_exe() -> str:
    """Absolute path of the ``ffmpeg`` binary inside the imageio-ffmpeg wheel.

    ``imageio_ffmpeg.get_ffmpeg_exe()`` falls back to a conda or ``PATH`` ffmpeg (and honours
    ``IMAGEIO_FFMPEG_EXE``); anything outside the wheel's ``binaries`` directory is refused.
    """
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    bin_dir = Path(str(resources.files("imageio_ffmpeg.binaries"))).resolve()
    if not os.path.isabs(exe):
        raise RuntimeError("imageio-ffmpeg has no bundled binary; refusing to use a host ffmpeg")
    real = Path(exe).resolve()
    if not real.is_file() or os.path.commonpath([real, bin_dir]) != str(bin_dir):
        raise RuntimeError("imageio-ffmpeg returned a non-bundled ffmpeg; refusing to use it")
    return str(real)


@lru_cache(maxsize=1)
def _imageio_version() -> str:
    import imageio_ffmpeg

    proc = subprocess.run(
        [bundled_ffmpeg_exe(), "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    match = re.search(r"ffmpeg version (\S+)", proc.stdout)
    version = match.group(1) if match else "unknown"
    return f"FFmpeg {version} (imageio-ffmpeg {imageio_ffmpeg.__version__})"


_LAYOUT_CHANNELS: dict[str, int] = {
    "mono": 1,
    "stereo": 2,
    "downmix": 2,
    "2.1": 3,
    "3.0": 3,
    "3.0(back)": 3,
    "4.0": 4,
    "quad": 4,
    "quad(side)": 4,
    "3.1": 4,
    "5.0": 5,
    "5.0(side)": 5,
    "4.1": 5,
    "5.1": 6,
    "5.1(side)": 6,
    "6.0": 6,
    "6.0(front)": 6,
    "hexagonal": 6,
    "6.1": 7,
    "6.1(back)": 7,
    "6.1(front)": 7,
    "7.0": 7,
    "7.0(front)": 7,
    "7.1": 8,
    "7.1(wide)": 8,
    "7.1(wide-side)": 8,
    "octagonal": 8,
}

_INPUT_RE = re.compile(r"^Input #0, (?P<container>.+?), from ", re.MULTILINE)
_DURATION_RE = re.compile(
    r"Duration: (?:(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)|N/A)"
    r"(?:, start: [-\d.]+)?, bitrate: (?:(?P<br>\d+) kb/s|N/A)"
)
_AUDIO_RE = re.compile(r"^\s*Stream #0:\d+\S*: Audio: (?P<rest>.+)$", re.MULTILINE)
_RATE_RE = re.compile(r"(\d+) Hz")
_KBPS_RE = re.compile(r"(\d+) kb/s")
_NCH_RE = re.compile(r"(\d+) channels")


def _run_ffmpeg(args: list[str], *, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bundled_ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _parse_channels(fields: list[str]) -> int:
    for field in fields:
        name = field.strip().split(" ")[0]
        if name in _LAYOUT_CHANNELS:
            return _LAYOUT_CHANNELS[name]
        m = _NCH_RE.fullmatch(field.strip())
        if m:
            return int(m.group(1))
    return 0


def _meta_imageio(path: Path, stream_index: int) -> StreamMeta:
    size = path.stat().st_size
    proc = _run_ffmpeg(["-i", str(path)], timeout=120)
    info = proc.stderr
    m_input = _INPUT_RE.search(info)
    if not m_input:
        raise AudioDecodeError(f"cannot open {path.name}: not a recognised media file")
    audio = [m.group("rest") for m in _AUDIO_RE.finditer(info)]
    if not audio:
        raise NoAudioStreamError("no audio stream")
    if stream_index >= len(audio):
        raise NoAudioStreamError(
            f"audio stream {stream_index} requested but the file has {len(audio)}"
        )
    fields = [f.strip() for f in audio[stream_index].split(",")]
    codec = fields[0].split(" ")[0]
    m_rate = _RATE_RE.search(audio[stream_index])
    sample_rate = int(m_rate.group(1)) if m_rate else 0
    channels = _parse_channels(fields[1:])
    m_kbps = _KBPS_RE.search(audio[stream_index])
    bit_rate: int | None = int(m_kbps.group(1)) * 1000 if m_kbps else None
    bit_rate = bit_rate or None

    duration: float | None = None
    m_dur = _DURATION_RE.search(info)
    if m_dur and m_dur.group("h") is not None:
        duration = (
            int(m_dur.group("h")) * 3600 + int(m_dur.group("m")) * 60 + float(m_dur.group("s"))
        )
    if bit_rate is None and m_dur and m_dur.group("br") and len(audio) == 1:
        bit_rate = int(m_dur.group("br")) * 1000

    # Require one decodable second of audio, like the PyAV path requires one frame.
    check = subprocess.run(
        [
            bundled_ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:a:{stream_index}",
            "-t",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if check.returncode != 0 or not check.stdout:
        raise AudioDecodeError(f"no decodable audio in {path.name}")
    if sample_rate <= 0 or channels <= 0:
        raise AudioDecodeError(f"invalid audio parameters in {path.name}")
    return StreamMeta(
        container=m_input.group("container"),
        codec=codec,
        duration=round(duration, 6) if duration is not None else None,
        sample_rate=sample_rate,
        channels=channels,
        bit_rate=bit_rate,
        stream_index=stream_index,
        audio_streams=len(audio),
        size=size,
    )


def _iter_imageio(
    path: Path, *, rate: int, stream_index: int, max_seconds: float | None
) -> Iterator[np.ndarray]:
    channels = _meta_imageio(path, stream_index).channels
    args = [
        bundled_ffmpeg_exe(),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
    ]
    if max_seconds is not None:
        args += ["-t", f"{max_seconds:.3f}"]
    args += ["-ac", str(channels), "-ar", str(rate), "-acodec", "pcm_f32le", "-f", "f32le", "-"]

    frame_bytes = 4 * channels
    block = max(frame_bytes, _PIPE_BLOCK - _PIPE_BLOCK % frame_bytes)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
        if proc.stdout is None:  # pragma: no cover - stdout=PIPE always sets it
            raise RuntimeError("ffmpeg stdout pipe missing")
        pending = b""
        try:
            while True:
                chunk = proc.stdout.read(block)
                if not chunk:
                    break
                data = pending + chunk
                usable = len(data) - len(data) % frame_bytes
                pending = data[usable:]
                if usable:
                    samples = np.frombuffer(data[:usable], dtype="<f4")
                    yield samples.reshape(-1, channels).T.copy()
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            returncode = proc.wait()
        if returncode != 0:
            err.seek(0)
            message = err.read(2000).decode("utf-8", "replace").strip().splitlines()
            reason = message[-1] if message else f"exit code {returncode}"
            raise AudioDecodeError(f"cannot decode {path.name}: {reason}")
