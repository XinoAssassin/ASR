"""Explicit local-model downloads started from Settings (AGENTS rule 6: never implicit).

Only registry models (``asr.providers.local.models.MODELS``) can be fetched. One download runs at
a time in a daemon thread; state lives in memory (a restart forgets a running download, and the
partial files left behind keep the model "missing" until it is downloaded again).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from asr.providers.local import models

log = logging.getLogger(__name__)

Status = Literal["missing", "downloading", "downloaded", "failed"]


@dataclass
class _State:
    status: Status
    error: str | None = None


class DownloadBusy(RuntimeError):
    """Another model is being downloaded."""


class ModelDownloads:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._active: str | None = None

    def status(self, key: str) -> tuple[Status, str | None]:
        with self._lock:
            state = self._states.get(key)
            if state is not None and state.status in ("downloading", "failed"):
                return state.status, state.error
        return ("downloaded" if models.is_downloaded(self.cache_dir, key) else "missing"), None

    def start(self, key: str, *, mirror: bool = False) -> None:
        spec = models.spec(key)  # KeyError for unknown keys
        with self._lock:
            if self._active is not None:
                raise DownloadBusy(f"{self._active} is being downloaded")
            if models.is_downloaded(self.cache_dir, key):
                return
            self._active = key
            self._states[key] = _State("downloading")
        use_mirror = mirror and spec.mirror is not None
        threading.Thread(
            target=self._run, args=(key, use_mirror), name=f"download-{key}", daemon=True
        ).start()

    def _run(self, key: str, mirror: bool) -> None:
        try:
            models.download(self.cache_dir, key, mirror=mirror)
            state = _State("downloaded")
        except Exception as exc:
            log.warning("download of %s failed: %s", key, type(exc).__name__)
            state = _State("failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        with self._lock:
            self._states[key] = state
            self._active = None
