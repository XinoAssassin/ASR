"""DashScope temporary upload: getPolicy → OSS PostObject → ``oss://`` URL.

Source: https://help.aliyun.com/zh/model-studio/get-temporary-file-url (verified 2026-10-29).

- ``GET {endpoint}/api/v1/uploads?action=getPolicy&model=<model>`` returns ``data`` with
  ``policy``, ``signature``, ``upload_dir``, ``upload_host``, ``expire_in_seconds``,
  ``max_file_size_mb``, ``capacity_limit_mb``, ``oss_access_key_id``, ``x_oss_object_acl``,
  ``x_oss_forbid_overwrite`` (numbers may arrive as strings).
- ``POST {upload_host}`` multipart/form-data with ``OSSAccessKeyId``, ``policy``, ``Signature``,
  ``key`` (= ``upload_dir/<file name>``), ``x-oss-object-acl``, ``x-oss-forbid-overwrite``,
  ``success_action_status=200`` and ``file`` as the **last** field. HTTP 200 on success.
- The file is then addressed as ``oss://<key>``; valid 48 h, bound to the same model name and
  Alibaba account. Requests that reference it need ``X-DashScope-OssResourceResolve: enable``.

The upload is streamed from disk (httpx reads the file object in chunks).
"""

from __future__ import annotations

import io
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from asr.providers.base import ProviderError
from asr.providers.cloud.client import DashScopeClient
from asr.providers.cloud.security import is_aliyun_https_url, redact_url

MB = 1024 * 1024
UPLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
UPLOAD_ATTEMPTS = 3
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class UploadPolicy(BaseModel):
    """The ``data`` object of the getPolicy response (secrets: policy, signature, key id)."""

    policy: str
    signature: str
    upload_dir: str
    upload_host: str
    oss_access_key_id: str
    x_oss_object_acl: str = "private"
    x_oss_forbid_overwrite: str = "true"
    expire_in_seconds: int | None = None
    max_file_size_mb: int = Field(gt=0)
    capacity_limit_mb: int | None = None

    @field_validator("expire_in_seconds", "max_file_size_mb", "capacity_limit_mb", mode="before")
    @classmethod
    def _int_from_str(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip():
            return int(float(v))
        return v

    @field_validator("x_oss_object_acl", "x_oss_forbid_overwrite", mode="before")
    @classmethod
    def _str(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return "true" if v else "false"
        return v

    @property
    def max_bytes(self) -> int:
        return self.max_file_size_mb * MB


def fetch_policy(client: DashScopeClient, model: str) -> UploadPolicy:
    data = client.get_policy(model)
    try:
        policy = UploadPolicy.model_validate(data)
    except ValueError:
        raise ProviderError("DashScope upload policy response is missing fields") from None
    if not is_aliyun_https_url(policy.upload_host):
        raise ProviderError(
            f"DashScope upload policy names an unexpected upload host: "
            f"{redact_url(policy.upload_host)}"
        )
    return policy


def safe_object_name(filename: str) -> str:
    """``<8 hex>-<ascii-safe name>``; unique so retries never hit ``x-oss-forbid-overwrite``."""
    path = Path(filename)
    stem = _SAFE_CHARS.sub("_", path.stem).strip("._") or "audio"
    suffix = _SAFE_CHARS.sub("", path.suffix.lstrip(".")).lower()[:10]
    name = f"{uuid.uuid4().hex[:8]}-{stem[:80]}"
    return f"{name}.{suffix}" if suffix else name


class _ProgressReader(io.RawIOBase):
    """Read-only file wrapper reporting bytes read; httpx streams it in chunks."""

    def __init__(self, raw: io.BufferedReader, on_read: Callable[[int], None]) -> None:
        super().__init__()
        self._raw = raw
        self._on_read = on_read
        self._done = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._raw.fileno()

    def tell(self) -> int:
        return self._raw.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        pos = self._raw.seek(offset, whence)
        self._done = pos
        return pos

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size)
        if chunk:
            self._done += len(chunk)
            self._on_read(self._done)
        return chunk

    def readinto(self, b: Any) -> int:  # pragma: no cover - httpx uses read()
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def upload_file(
    client: DashScopeClient,
    policy: UploadPolicy,
    path: Path,
    *,
    model: str,
    filename: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Upload ``path`` and return its ``oss://`` URL.

    Transient failures (connection errors, 5xx) are retried with a fresh policy and a fresh
    object key. ``on_progress(sent, total)`` is called while the body streams; exceptions it
    raises (e.g. ``JobCancelled``) abort the upload.
    """
    total = path.stat().st_size
    if total > policy.max_bytes:
        raise ProviderError(
            f"Audio is {total / MB:.0f} MB, above the {policy.max_file_size_mb} MB upload limit"
        )
    name = filename or path.name
    current = policy
    last_error = "unknown error"
    for attempt in range(UPLOAD_ATTEMPTS):
        if attempt:
            current = fetch_policy(client, model)
        key = f"{current.upload_dir.rstrip('/')}/{safe_object_name(name)}"
        form = {
            "OSSAccessKeyId": current.oss_access_key_id,
            "policy": current.policy,
            "Signature": current.signature,
            "key": key,
            "x-oss-object-acl": current.x_oss_object_acl,
            "x-oss-forbid-overwrite": current.x_oss_forbid_overwrite,
            "success_action_status": "200",
        }

        def report(sent: int) -> None:
            if on_progress is not None:
                on_progress(min(sent, total), total)

        try:
            with path.open("rb") as raw:
                reader = _ProgressReader(raw, report)
                response = client.http.post(
                    current.upload_host,
                    data=form,
                    files={"file": (key.rsplit("/", 1)[-1], reader, "application/octet-stream")},
                    timeout=UPLOAD_TIMEOUT,
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = type(exc).__name__
            continue
        if response.status_code in (200, 201, 204):
            return f"oss://{key}"
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        # 4xx: OSS returns XML (e.g. Policy expired, EntityTooLarge). Report the status only.
        raise ProviderError(
            f"Temporary upload to Alibaba OSS was rejected (HTTP {response.status_code})"
        )
    raise ProviderError(f"Temporary upload to Alibaba OSS failed: {last_error}")
