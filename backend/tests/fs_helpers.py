"""Filesystem helpers for tests that need symlinks or Windows junctions.

Windows without Developer Mode (or admin) refuses ``symlink_to`` with WinError 1314; such tests
skip there instead of erroring. Junctions need no privilege and cover the same escape on Windows.
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path

import pytest

_ERROR_PRIVILEGE_NOT_HELD = 1314


def symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    """``link.symlink_to(target)``, or skip the test where symlinks are not permitted."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError as exc:
        pytest.skip(f"symlinks are not supported here: {exc}")
    except OSError as exc:
        denied = getattr(exc, "winerror", None) == _ERROR_PRIVILEGE_NOT_HELD or exc.errno in (
            errno.EPERM,
            errno.EACCES,
        )
        if not denied:
            raise
        pytest.skip(f"symlinks are not permitted here: {exc}")


def junction_or_skip(link: Path, target: Path) -> None:
    """Windows directory junction ``link`` -> ``target`` (no privilege needed); skip elsewhere."""
    if sys.platform != "win32":
        pytest.skip("junctions exist only on Windows")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))
