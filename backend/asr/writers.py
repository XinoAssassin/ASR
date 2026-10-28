"""Transcript export: txt / srt / vtt / json. Owned by ``backend-jobs`` (M1)."""

from typing import Literal

from asr.providers.base import Transcript

ExportFormat = Literal["txt", "srt", "vtt", "json"]

MEDIA_TYPES: dict[str, str] = {
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "json": "application/json",
}


def render(
    transcript: Transcript,
    fmt: ExportFormat,
    *,
    timestamps: bool = True,
    merge_same_speaker: bool = False,
) -> str:
    """Render ``transcript`` using speaker display names from ``transcript.speakers``."""
    raise NotImplementedError
