"""Request body size cap for the JSON API.

Every route except the upload routes takes a small JSON body, so anything larger than
``MAX_JSON_BODY`` is refused with 413 before it is buffered: by ``Content-Length`` up front, and
by counting the streamed bytes for chunked / unannounced bodies. Only the two routes that stream
file bytes are exempt and keep their own limits: ``POST /api/uploads`` (2 GiB file cap) and
``PUT /api/uploads/{session_id}/parts/{n}`` (exact part sizes). The JSON upload routes
(``init``, ``register``) are capped like every other route.
"""

import re
from collections.abc import Sequence

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_JSON_BODY = 2 * 1024 * 1024  # bytes
# (method, full-path pattern) of the routes whose body is a file, not JSON.
EXEMPT_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"/api/uploads")),
    ("PUT", re.compile(r"/api/uploads/[^/]+/parts/[^/]+")),
)
_DETAIL = "request body too large (max 2 MB)"


class BodyTooLarge(HTTPException):
    """Raised from ``receive`` once the streamed body passes the cap. A Starlette
    ``HTTPException`` so FastAPI's body parsing re-raises it as is (not as a 400)."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=_DETAIL)


def _exempt(scope: Scope, routes: Sequence[tuple[str, re.Pattern[str]]]) -> bool:
    method, path = scope.get("method"), scope["path"]
    return any(method == m and pattern.fullmatch(path) for m, pattern in routes)


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = MAX_JSON_BODY,
        exempt_routes: Sequence[tuple[str, re.Pattern[str]]] = EXEMPT_ROUTES,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_routes = tuple(exempt_routes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _exempt(scope, self.exempt_routes):
            await self.app(scope, receive, send)
            return

        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.max_bytes:
                    status = 413 if declared > self.max_bytes else 400
                    detail = _DETAIL if status == 413 else "invalid Content-Length"
                    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
                    return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            # Raised outside the app's exception handlers (e.g. by a middleware reading the body).
            if started:
                raise
            await JSONResponse({"detail": _DETAIL}, status_code=413)(scope, receive, send)
