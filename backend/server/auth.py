"""Shared ``X-API-Key`` check (PLAN §4.1, EXECUTION_LOG D6).

Implemented as ASGI middleware so every route is protected unless explicitly exempt. The key is
read from the header only, never from the query string. Installed inside ``CORSMiddleware`` so
401 responses still carry CORS headers.

An empty ``ASR_API_KEY`` disables the key check (single-machine use). Open mode then adds:

- the TCP peer (``scope["client"]``) must be a loopback address, unless
  ``ASR_ALLOW_UNAUTHENTICATED_NETWORK`` is set (Docker: the peer is the bridge gateway, and
  compose publishes the port on 127.0.0.1 only). The ``Host`` header alone proves nothing: any
  LAN client can send ``Host: localhost``;
- the ``Host`` header must be a loopback name or listed in ``ASR_ALLOWED_HOSTS`` (DNS rebinding);
- state-changing requests carrying an ``Origin`` that is neither in ``ASR_CORS_ORIGINS`` nor a
  loopback origin are refused (cross-site "simple" requests such as a multipart form POST bypass
  CORS preflight; a page on another site can never send a loopback ``Origin``);
- requests carrying proxy headers (``Forwarded``, ``X-Forwarded-For``, ``X-Real-IP``, ``Via``)
  are refused: a proxy relays other machines' requests, so it needs ``ASR_API_KEY``;
- a peer without an address (Unix domain socket) is not trusted as loopback, since such sockets
  are normally fronted by a proxy.

What open mode does *not* protect against: anything on this machine that relays remote clients
over a new loopback connection without proxy headers - ``nginx proxy_pass`` without
``X-Forwarded-For``, ``netsh interface portproxy``, ``socat``, ``ssh -R``, ``pnpm dev --host``.
Their requests arrive from 127.0.0.1 with a loopback ``Host`` and are indistinguishable from
local ones. Any proxy or forwarder in front of the backend therefore requires ``ASR_API_KEY``.

The key is compared as raw header bytes against its UTF-8 encoding (ASGI headers are bytes);
browsers can only send ISO-8859-1 header values, so a usable key is printable ASCII.
"""

import hmac
import ipaddress
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
PROXY_HEADERS: frozenset[bytes] = frozenset(
    {b"forwarded", b"x-forwarded-for", b"x-real-ip", b"via"}
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _raw_header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return bytes(value)
    return None


def _header(scope: Scope, name: bytes) -> str | None:
    value = _raw_header(scope, name)
    return None if value is None else value.decode("latin-1")


def _has_proxy_headers(scope: Scope) -> bool:
    return any(key in PROXY_HEADERS for key, _ in scope.get("headers", ()))


def _is_loopback_origin(origin: str) -> bool:
    scheme, sep, rest = origin.strip().lower().partition("://")
    return bool(sep) and scheme in ("http", "https") and host_name(rest) in LOOPBACK_HOSTS


def is_loopback_address(host: str) -> bool:
    """``localhost`` or a loopback IP literal (IPv4-mapped IPv6 included)."""
    host = host.strip().lower().strip("[]")
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def _peer_is_loopback(scope: Scope) -> bool:
    client = scope.get("client")
    if client is None:  # Unix domain socket: on this machine, but almost always a proxy's
        return False
    return is_loopback_address(str(client[0]))


def host_name(host_header: str) -> str:
    """``"[::1]:8000" -> "::1"``, ``"Example.com:80" -> "example.com"``."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def api_key_matches(provided: str | bytes | None, expected: SecretStr) -> bool:
    """Constant-time comparison; ``False`` when nothing was provided.

    ``provided`` is the raw header value (bytes) or text; both are compared as UTF-8 bytes, so a
    non-ASCII key works for clients that send it UTF-8 encoded (decoding the header as latin-1
    and re-encoding it as UTF-8 never matched)."""
    if provided is None:
        return False
    raw = provided.encode("utf-8") if isinstance(provided, str) else provided
    return hmac.compare_digest(raw, expected.get_secret_value().encode("utf-8"))


def is_browser_safe_key(key: str) -> bool:
    """Printable ASCII only: what browsers (``fetch``) can put into an ``X-API-Key`` header."""
    return all(" " <= ch <= "~" for ch in key)


class ApiKeyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key: SecretStr,
        allowed_hosts: Sequence[str] = (),
        allowed_origins: Sequence[str] = (),
        allow_network: bool = False,
    ) -> None:
        self.app = app
        self.api_key = api_key
        self.enabled = bool(api_key.get_secret_value())
        self.allow_network = allow_network
        self.allowed_hosts = LOOPBACK_HOSTS | {h.lower() for h in allowed_hosts}
        self.allowed_origins = frozenset(o.rstrip("/") for o in allowed_origins)

    def _open_mode_rejection(self, scope: Scope) -> JSONResponse | None:
        if not self.allow_network and not _peer_is_loopback(scope):
            return JSONResponse(
                {
                    "detail": "API authentication is off, so only clients on this machine are "
                    "accepted; set ASR_API_KEY on the server"
                },
                status_code=403,
            )
        if _has_proxy_headers(scope):
            return JSONResponse(
                {
                    "detail": "API authentication is off and this request came through a proxy; "
                    "set ASR_API_KEY on the server to serve clients through a proxy"
                },
                status_code=403,
            )
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
        if not self.enabled and scope["type"] in ("http", "websocket"):
            rejection = self._open_mode_rejection(scope)
            if rejection is not None:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
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

        provided = _raw_header(scope, _HEADER_BYTES)

        if api_key_matches(provided, self.api_key):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        await response(scope, receive, send)
