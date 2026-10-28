"""Shared ``X-API-Key`` check (PLAN §4.1, EXECUTION_LOG D6).

Implemented as ASGI middleware so every route is protected unless explicitly exempt. The key is
read from the header only, never from the query string. Installed inside ``CORSMiddleware`` so
401 responses still carry CORS headers.

An empty ``ASR_API_KEY`` disables the key check (single-machine use). Because any web page the user
visits can then reach the API, open mode adds two browser-facing guards:

- the ``Host`` header must be a loopback name or listed in ``ASR_ALLOWED_HOSTS`` (DNS rebinding);
- state-changing requests carrying an ``Origin`` that is neither in ``ASR_CORS_ORIGINS`` nor a
  loopback origin are refused (cross-site "simple" requests such as a multipart form POST bypass
  CORS preflight; a page on another site can never send a loopback ``Origin``).
"""

import hmac
from collections.abc import Sequence

from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_HEADER = "X-API-Key"
_HEADER_BYTES = API_KEY_HEADER.lower().encode("latin-1")

EXEMPT_PATHS: frozenset[str] = frozenset(
    {"/api/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _is_loopback_origin(origin: str) -> bool:
    scheme, sep, rest = origin.strip().lower().partition("://")
    return bool(sep) and scheme in ("http", "https") and host_name(rest) in LOOPBACK_HOSTS


def host_name(host_header: str) -> str:
    """``"[::1]:8000" -> "::1"``, ``"Example.com:80" -> "example.com"``."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def api_key_matches(provided: str | None, expected: SecretStr) -> bool:
    """Constant-time comparison; ``False`` when nothing was provided."""
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    )


class ApiKeyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key: SecretStr,
        allowed_hosts: Sequence[str] = (),
        allowed_origins: Sequence[str] = (),
    ) -> None:
        self.app = app
        self.api_key = api_key
        self.enabled = bool(api_key.get_secret_value())
        self.allowed_hosts = LOOPBACK_HOSTS | {h.lower() for h in allowed_hosts}
        self.allowed_origins = frozenset(o.rstrip("/") for o in allowed_origins)

    def _open_mode_rejection(self, scope: Scope) -> JSONResponse | None:
        if "*" not in self.allowed_hosts:
            host = _header(scope, b"host")
            if host is None or host_name(host) not in self.allowed_hosts:
                return JSONResponse(
                    {"detail": "Host not allowed; set ASR_ALLOWED_HOSTS or ASR_API_KEY"},
                    status_code=403,
                )
        origin = _header(scope, b"origin")
        if (
            scope.get("method") not in _SAFE_METHODS
            and origin is not None
            and origin.rstrip("/") not in self.allowed_origins
            and not _is_loopback_origin(origin)
        ):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled and scope["type"] == "http":
            rejection = self._open_mode_rejection(scope)
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        if (
            not self.enabled
            or scope["type"] not in ("http", "websocket")
            or scope.get("method") == "OPTIONS"  # CORS preflight never carries credentials
            or scope["path"] in EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        provided = _header(scope, _HEADER_BYTES)

        if api_key_matches(provided, self.api_key):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        await response(scope, receive, send)
