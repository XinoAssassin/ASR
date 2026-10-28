"""Accepted upload formats (PLAN §6.2). Decoding is attempted by PyAV for any of these."""

from pathlib import PurePath

ACCEPTED_EXTENSIONS: frozenset[str] = frozenset(
    {"m4a", "mp3", "wav", "flac", "aac", "caf", "ogg", "opus", "webm", "mp4", "m4v", "mov", "mkv"}
)


def is_accepted(filename: str) -> bool:
    """True if ``filename`` has an accepted extension (case-insensitive, no leading dot)."""
    suffix = PurePath(filename).suffix
    return suffix[1:].lower() in ACCEPTED_EXTENSIONS if suffix else False
