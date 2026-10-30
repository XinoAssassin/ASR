"""Transcoding through the bundled FFmpeg. Owned by ``backend-core`` (M1).

Used for the browser player (cached MP3, EXECUTION_LOG D12) and for cloud uploads (16 kHz FLAC,
D8). Output is written to a temporary file next to ``dst`` and renamed into place, so readers
never see a partial file. Backend follows ``ASR_DECODER`` like ``asr.audio.decode``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import av
import numpy as np

from asr.audio import decode
from asr.audio.decode import AudioDecodeError

TranscodeFormat = Literal["flac", "mp3"]

MP3_RATES: tuple[int, ...] = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
FLAC_MAX_CHANNELS = 8
# Speech-appropriate MP3 bit rates for the browser player.
BROWSER_MP3_BITRATE = {1: 64_000, 2: 96_000}

_ENCODERS: dict[str, str] = {"flac": "flac", "mp3": "libmp3lame"}


def transcode_audio(
    src: Path,
    dst: Path,
    *,
    fmt: TranscodeFormat,
    sample_rate: int,
    channels: int | None,
    stream_index: int = 0,
    bitrate: int | None = None,
    channel: int | None = None,
) -> Path:
    """Transcode one audio stream of ``src`` into ``dst``; ``channels=None`` keeps the layout.

    ``channels=1`` downmixes (channel mean); ``channel=k`` instead extracts source channel ``k``
    as mono (``channels`` must then be ``None`` or 1). MP3 carries at most two channels and only the
    MPEG sample rates in ``MP3_RATES``; FLAC is 16-bit. ``bitrate`` (bits/s) applies to MP3
    only. Returns ``dst``. Raises ``ValueError`` for invalid parameters and
    ``AudioDecodeError`` when ``src`` cannot be decoded.
    """
    src, dst = Path(src), Path(dst)
    if fmt not in _ENCODERS:
        raise ValueError(f"unsupported format: {fmt!r}")
    if fmt == "mp3" and sample_rate not in MP3_RATES:
        raise ValueError(f"MP3 does not support {sample_rate} Hz")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    meta = decode.stream_meta(src, stream_index)
    if channel is not None:
        if channels not in (None, 1):
            raise ValueError("channel=k produces mono output; channels must be None or 1")
        if not 0 <= channel < meta.channels:
            raise ValueError(f"channel {channel} out of range for {meta.channels} channels")
        channels = 1
    out_channels = meta.channels if channels is None else channels
    if out_channels not in (1, meta.channels):
        raise ValueError(f"channels must be None or 1 (or {meta.channels}), got {channels}")
    max_channels = 2 if fmt == "mp3" else FLAC_MAX_CHANNELS
    if out_channels > max_channels:
        raise ValueError(f"{fmt} supports at most {max_channels} channels")

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if decode.decoder_backend() == "imageio":
            _transcode_imageio(
                src,
                tmp,
                fmt=fmt,
                sample_rate=sample_rate,
                in_channels=meta.channels,
                out_channels=out_channels,
                stream_index=stream_index,
                bitrate=bitrate,
                channel=channel,
            )
        else:
            _transcode_pyav(
                src,
                tmp,
                fmt=fmt,
                sample_rate=sample_rate,
                out_channels=out_channels,
                stream_index=stream_index,
                bitrate=bitrate,
                channel=channel,
            )
        with contextlib.suppress(OSError):  # mkstemp creates 0600; drvfs refuses chmod
            tmp.chmod(0o644)
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return dst


def browser_params(sample_rate: int, channels: int) -> tuple[int, int | None, int]:
    """``(mp3_rate, channels_arg, bitrate)`` for the browser player transcode."""
    if sample_rate in MP3_RATES:
        rate = sample_rate
    elif sample_rate > MP3_RATES[-1]:
        rate = MP3_RATES[-1]
    else:
        rate = next(r for r in MP3_RATES if r >= sample_rate)
    out_channels = channels if channels in (1, 2) else 1
    channels_arg = None if out_channels == channels else 1
    return rate, channels_arg, BROWSER_MP3_BITRATE[out_channels]


def transcode_for_browser(src: Path, dst: Path, *, stream_index: int = 0) -> Path:
    """MP3 transcode for the browser player (EXECUTION_LOG D12).

    Mono stays mono and stereo stays stereo (per-channel recordings remain audible as such);
    sources with more channels are downmixed to mono. The sample rate is kept when MP3
    supports it (otherwise the nearest MPEG rate); 64 kbit/s mono, 96 kbit/s stereo.
    ``stream_index`` picks the audio stream (the one the job transcribed).
    """
    meta = decode.stream_meta(Path(src), stream_index)
    rate, channels_arg, bitrate = browser_params(meta.sample_rate, meta.channels)
    return transcode_audio(
        src,
        dst,
        fmt="mp3",
        sample_rate=rate,
        channels=channels_arg,
        bitrate=bitrate,
        stream_index=stream_index,
    )


def _layout_name(channels: int) -> str:
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels} channels")


def _transcode_pyav(
    src: Path,
    tmp: Path,
    *,
    fmt: TranscodeFormat,
    sample_rate: int,
    out_channels: int,
    stream_index: int,
    bitrate: int | None,
    channel: int | None = None,
) -> None:
    layout = _layout_name(out_channels)
    written = 0
    with av.open(str(tmp), mode="w", format=fmt) as out:
        stream = out.add_stream(_ENCODERS[fmt], rate=sample_rate, layout=layout)
        if fmt == "mp3":
            stream.bit_rate = bitrate or BROWSER_MP3_BITRATE.get(out_channels, 96_000)
        for block in decode.iter_decoded(src, rate=sample_rate, stream_index=stream_index):
            if channel is not None:
                block = block[channel : channel + 1]
            elif out_channels == 1 and block.shape[0] > 1:
                block = block.mean(axis=0, dtype=np.float32, keepdims=True)
            if block.shape[0] != out_channels:
                raise AudioDecodeError(f"channel count changed while decoding {src.name}")
            block = np.ascontiguousarray(np.clip(block, -1.0, 1.0), dtype=np.float32)
            frame = av.AudioFrame.from_ndarray(block, format="fltp", layout=layout)
            frame.rate = sample_rate
            frame.pts = written
            written += block.shape[1]
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    if written == 0:
        raise AudioDecodeError(f"no decodable audio in {src.name}")


def _transcode_imageio(
    src: Path,
    tmp: Path,
    *,
    fmt: TranscodeFormat,
    sample_rate: int,
    in_channels: int,
    out_channels: int,
    stream_index: int,
    bitrate: int | None,
    channel: int | None = None,
) -> None:
    args = [
        decode.bundled_ffmpeg_exe(),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        f"0:a:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
    ]
    if channel is not None:
        args += ["-af", f"pan=mono|c0=c{channel}"]
    elif out_channels == 1 and in_channels > 1:
        # Plain channel mean; ``-ac 1`` would use FFmpeg's -3 dB-per-channel downmix matrix.
        gain = 1.0 / in_channels
        terms = "+".join(f"{gain:.6f}*c{i}" for i in range(in_channels))
        args += ["-af", f"pan=mono|c0={terms}"]
    args += ["-ar", str(sample_rate), "-ac", str(out_channels), "-c:a", _ENCODERS[fmt]]
    if fmt == "mp3":
        args += ["-b:a", str(bitrate or BROWSER_MP3_BITRATE.get(out_channels, 96_000))]
    else:
        args += ["-sample_fmt", "s16"]
    args += ["-f", fmt, str(tmp)]
    proc = subprocess.run(args, capture_output=True, timeout=6 * 3600, check=False)
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = lines[-1] if lines else f"exit code {proc.returncode}"
        raise AudioDecodeError(f"cannot transcode {src.name}: {reason}")
