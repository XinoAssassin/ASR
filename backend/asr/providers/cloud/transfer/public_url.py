"""User-supplied public URL (PLAN §4.4 c, EXECUTION_LOG D3).

The backend never fetches the URL; DashScope downloads it. We only check the scheme so that
nothing but a plain http(s) URL reaches the API.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from asr.providers.base import ProviderError


def validate_public_url(url: str) -> str:
    """Return ``url`` unchanged if it is an absolute http(s) URL with a host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ProviderError("The audio URL is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProviderError("The audio URL must be an absolute http(s) URL")
    if any(c.isspace() for c in url):
        raise ProviderError("The audio URL must not contain whitespace")
    return url
