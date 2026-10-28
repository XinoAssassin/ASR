"""Shared ``X-API-Key`` check (PLAN §4.1, EXECUTION_LOG D6).

Implemented as ASGI middleware so every route is protected unless explicitly exempt. The key is
read from the header only, never from the query string. An empty ``ASR_API_KEY`` disables the
check. Installed inside ``CORSMiddleware`` so 401 responses still carry CORS headers.
"""

import hmac

from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_HEADER = "X-API-Key"
_HEADER_BYTES = API_KEY_HEADER.lower().encode("latin-1")

EXEMPT_PATHS: frozenset[str] = frozenset(
    {"/api/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)


def api_key_matches(provided: str | None, expected: SecretStr) -> bool:
    """Constant-time comparison; ``False`` when nothing was provided."""
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    )


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, *, api_key: SecretStr) -> None:
        self.app = app
        self.api_key = api_key
        self.enabled = bool(api_key.get_secret_value())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled
            or scope["type"] not in ("http", "websocket")
            or scope.get("method") == "OPTIONS"  # CORS preflight never carries credentials
            or scope["path"] in EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        provided: str | None = None
        for name, value in scope.get("headers", ()):
            if name == _HEADER_BYTES:
                provided = value.decode("latin-1")
                break

        if api_key_matches(provided, self.api_key):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        await response(scope, receive, send)
