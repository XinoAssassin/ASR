"""Endpoint allow-list, result-URL checks and redaction for the DashScope provider.

The API key is only ever sent to an allow-listed endpoint (EXECUTION_LOG D2). Result files are
downloaded without credentials and only from HTTPS ``*.aliyuncs.com`` hosts (SSRF hardening).
User-facing messages and log lines go through ``redact`` so signed URLs (``Signature=``,
``OSSAccessKeyId=``) and the key never leak.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

ALLOWED_ENDPOINTS: tuple[str, ...] = (
    "https://dashscope.aliyuncs.com",
    "https://cn-beijing.maas.qianwenaiapi.com",
)
"""Endpoints the DashScope key may be sent to. Workspace hosts are matched by pattern."""

_WORKSPACE_HOST = re.compile(r"^[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com$")
_ALLOWED_HOSTS = frozenset(urlsplit(e).hostname for e in ALLOWED_ENDPOINTS)
_ALIYUN_SUFFIX = ".aliyuncs.com"
_URL_QUERY = re.compile(r"((?:https?|oss)://[^\s?#\"'<>]+)[?#][^\s\"'<>]*", re.IGNORECASE)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_SK = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
MAX_MESSAGE_CHARS = 300


def _plain_host(netloc: str) -> str | None:
    """``netloc`` if it is a bare lowercase host name (no userinfo, no port), else ``None``."""
    if not netloc or "@" in netloc or ":" in netloc or netloc != netloc.lower():
        return None
    if any(c.isspace() for c in netloc):
        return None
    return netloc


def is_allowed_endpoint(url: str) -> bool:
    """True for ``ALLOWED_ENDPOINTS`` and ``https://{workspace}.cn-beijing.maas.aliyuncs.com``.

    The URL must be exactly ``https://<host>`` (a single trailing ``/`` is tolerated): no
    userinfo, port, path, query or fragment.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    if any(c.isspace() or ord(c) < 0x20 for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.path not in ("", "/") or parts.query or parts.fragment:
        return False
    if "?" in url or "#" in url:
        return False
    host = _plain_host(parts.netloc)
    if host is None:
        return False
    return host in _ALLOWED_HOSTS or bool(_WORKSPACE_HOST.match(host))


def normalize_endpoint(url: str) -> str:
    """Endpoint without a trailing slash (call only after ``is_allowed_endpoint``)."""
    return url.rstrip("/")


def is_aliyun_https_url(url: str) -> bool:
    """True for ``https://<host>.aliyuncs.com/...`` without userinfo or a non-443 port."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    return host.endswith(_ALIYUN_SUFFIX) and len(host) > len(_ALIYUN_SUFFIX)


def redact(
    text: str, secrets: tuple[str, ...] = (), *, max_chars: int | None = MAX_MESSAGE_CHARS
) -> str:
    """Strip query strings from URLs, bearer tokens, ``sk-`` keys and the given secrets.

    The result is cut to ``max_chars`` (user-facing messages); ``None`` keeps it whole (logs).
    """
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    out = _URL_QUERY.sub(r"\1?<redacted>", out)
    out = _BEARER.sub(r"\1***", out)
    out = _SK.sub("sk-***", out)
    if max_chars is not None and len(out) > max_chars:
        out = out[: max_chars - 1] + "…"
    return out


def redact_url(url: str) -> str:
    """``scheme://host/path`` without query or fragment, for logs and messages."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{parts.path}"


class RedactingFilter(logging.Filter):
    """Strips URL query strings (signatures) from ``httpx`` / ``httpcore`` log records.

    httpx logs every request URL at INFO; result URLs carry ``Signature`` / ``OSSAccessKeyId``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record; leave it alone
            return True
        cleaned = _URL_QUERY.sub(r"\1?<redacted>", message)
        cleaned = _BEARER.sub(r"\1***", cleaned)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


_FILTER = RedactingFilter()


def install_log_redaction() -> None:
    """Attach ``RedactingFilter`` to the HTTP client loggers (idempotent)."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
