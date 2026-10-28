"""Alibaba DashScope ``qwen-audio-3.1-asr-flash-filetrans`` provider (EXECUTION_LOG §3.3).

Owned by the ``cloud-provider`` agent (M2). Stub: signatures only.
"""

from pathlib import Path
from typing import Literal

import httpx

from asr.providers.base import (
    AudioSource,
    ConnectionTestResult,
    FileTransferMode,
    JobOptions,
    ProgressCallback,
    Transcript,
)

ALLOWED_ENDPOINTS: tuple[str, ...] = (
    "https://dashscope.aliyuncs.com",
    "https://cn-beijing.maas.qianwenaiapi.com",
)


def is_allowed_endpoint(url: str) -> bool:
    """True for ``ALLOWED_ENDPOINTS`` and ``https://{workspace}.cn-beijing.maas.aliyuncs.com``."""
    raise NotImplementedError


class DashScopeFiletransProvider:
    name: Literal["cloud"] = "cloud"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str,
        work_dir: Path,
        file_transfer: FileTransferMode = "temp_upload",
        http: httpx.Client | None = None,
        poll_interval: float = 3.0,
        timeout_s: float = 6 * 3600,
    ) -> None:
        raise NotImplementedError

    def transcribe(
        self, source: AudioSource, options: JobOptions, progress: ProgressCallback
    ) -> Transcript:
        raise NotImplementedError


def test_connection(
    *, api_key: str, endpoint: str, model: str, http: httpx.Client | None = None
) -> ConnectionTestResult:
    """Verify the DashScope key against ``endpoint`` (e.g. by requesting an upload policy)."""
    raise NotImplementedError


# Not a pytest test, even when imported into a test module.
test_connection.__test__ = False  # type: ignore[attr-defined]
