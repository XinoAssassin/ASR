"""Probe and decode via the bundled FFmpeg (docs/research/bundled-ffmpeg.md).

Owned by ``backend-core`` (M1). Backend selected by ``ASR_DECODER=pyav|imageio``.
"""

from pathlib import Path

import av
import numpy as np

from asr.audio.types import ProbeInfo


def probe(path: Path, stream_index: int = 0) -> ProbeInfo:
    """Container / stream metadata for the ``stream_index``-th audio stream."""
    raise NotImplementedError


def decode_pcm16k(path: Path, *, channel: int | None = None, stream_index: int = 0) -> np.ndarray:
    """Decode to float32 mono 16 kHz in [-1, 1]; ``channel`` selects one input channel."""
    raise NotImplementedError


def ffmpeg_version() -> str:
    """Human-readable version of the bundled FFmpeg."""
    return f"FFmpeg {av.ffmpeg_version_info} (PyAV {av.__version__})"
