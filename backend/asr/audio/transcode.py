"""Transcoding through the bundled FFmpeg. Owned by ``backend-core`` (M1)."""

from pathlib import Path
from typing import Literal


def transcode_audio(
    src: Path,
    dst: Path,
    *,
    fmt: Literal["flac", "mp3"],
    sample_rate: int,
    channels: int | None,
    stream_index: int = 0,
    bitrate: int | None = None,
) -> Path:
    """Transcode one audio stream of ``src`` into ``dst``; ``channels=None`` keeps the layout."""
    raise NotImplementedError


def transcode_for_browser(src: Path, dst: Path) -> Path:
    """MP3 transcode for the browser player (EXECUTION_LOG D12)."""
    raise NotImplementedError
