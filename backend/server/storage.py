"""Upload-root validation and file persistence. Owned by ``backend-core`` (M1).

AGENTS.md rule 2: every user-supplied path must resolve (realpath) inside one of
``EnvSettings.upload_roots``; reject ``..``, absolute paths outside roots and symlink escapes.
Chunked-upload sessions live on disk (``ASR_DATA_DIR``), not in the database.
"""

from collections.abc import Sequence
from pathlib import Path


class PathNotAllowedError(Exception):
    """The path resolves outside every upload root (HTTP 403)."""


def resolve_in_roots(path: str, roots: Sequence[Path]) -> Path:
    """Resolve ``path`` (absolute, or relative to ``roots[0]``) and require it inside a root."""
    raise NotImplementedError


def safe_filename(name: str) -> str:
    """Strip path separators and unsafe characters from a client-supplied filename."""
    raise NotImplementedError
