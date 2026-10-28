"""FastAPI application factory.

``uvicorn server.app:app`` serves the module-level app built from the process environment.
Tests call ``create_app(env)`` with an explicit ``EnvSettings`` and run the lifespan
(``asgi_lifespan.LifespanManager``).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from asr import __version__
from server.auth import API_KEY_HEADER, EXEMPT_PATHS, ApiKeyMiddleware
from server.config import EnvSettings, ensure_dirs, get_env
from server.db import Database
from server.jobs import JobManager
from server.routes import ROUTERS

CORS_ALLOW_HEADERS = [API_KEY_HEADER, "Content-Type", "Range", "Last-Event-ID"]
CORS_EXPOSE_HEADERS = ["Content-Range", "Accept-Ranges", "Content-Length", "Content-Disposition"]
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def create_app(env: EnvSettings | None = None) -> FastAPI:
    env = env or get_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The core library reads ASR_DECODER from the process env; keep it in sync with
        # values that came from a .env file.
        os.environ["ASR_DECODER"] = env.decoder
        ensure_dirs(env)
        db = await Database.open(env.db_path)
        job_manager = JobManager(db=db, env=env)
        app.state.db = db
        app.state.job_manager = job_manager
        try:
            await job_manager.start()
            yield
        finally:
            await job_manager.stop()
            await db.close()

    app = FastAPI(
        title="Multi-Speaker ASR",
        version=__version__,
        description="Speaker-labelled, timestamped transcription (local and cloud providers).",
        lifespan=lifespan,
    )
    app.state.env = env

    for router in ROUTERS:
        app.include_router(router)

    # Order matters: the last middleware added is the outermost. CORS wraps auth so that 401
    # responses carry CORS headers and preflight requests are answered before auth runs.
    app.add_middleware(ApiKeyMiddleware, api_key=env.api_key)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=env.cors_origins,
        allow_credentials=False,
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=CORS_EXPOSE_HEADERS,
        max_age=600,
    )

    _document_api_key(app)
    return app


def _document_api_key(app: FastAPI) -> None:
    """Advertise the X-API-Key header in OpenAPI (enforced by ``ApiKeyMiddleware``)."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = FastAPI.openapi(app)
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes["ApiKeyAuth"] = {"type": "apiKey", "in": "header", "name": API_KEY_HEADER}
        schema["security"] = [{"ApiKeyAuth": []}]
        for path, item in schema.get("paths", {}).items():
            if path in EXEMPT_PATHS:
                for operation in item.values():
                    operation["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


app = create_app()
