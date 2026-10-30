"""FastAPI application factory.

``uvicorn server.app:app`` serves the module-level app built from the process environment.
Tests call ``create_app(env)`` with an explicit ``EnvSettings`` and run the lifespan
(``asgi_lifespan.LifespanManager``).

The backend is a single process (job queue, SSE subscribers and run state live in memory; SQLite
is the store): the lifespan takes an advisory lock on ``<ASR_DATA_DIR>/server.lock`` and refuses to
start while another server holds it (docs/DEPLOYMENT.md, ARCH_REVIEW §4-d).
"""

import contextlib
import errno
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from asr import __version__
from server.auth import API_KEY_HEADER, EXEMPT_PATHS, ApiKeyMiddleware
from server.body_limit import MAX_JSON_BODY, BodySizeLimitMiddleware
from server.config import EnvSettings, apply_core_environ, ensure_dirs, get_env
from server.db import Database
from server.hardware import HardwareCache, warm_up
from server.jobs import JobManager
from server.logging_setup import SecretRedactingFilter, configure_logging
from server.model_downloads import ModelDownloads
from server.routes import ROUTERS

log = logging.getLogger(__name__)

CORS_ALLOW_HEADERS = [API_KEY_HEADER, "Content-Type", "Range", "Last-Event-ID"]
CORS_EXPOSE_HEADERS = ["Content-Range", "Accept-Ranges", "Content-Length", "Content-Disposition"]
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

LOCK_FILE = "server.lock"
# errno values meaning "another handle holds the lock" (flock: EWOULDBLOCK; msvcrt: EACCES).
_LOCK_HELD = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK})


class DataDirInUse(RuntimeError):
    """Another server process already serves this ``ASR_DATA_DIR``."""


class DataDirLock:
    """Advisory, non-blocking exclusive lock on ``<data_dir>/server.lock``.

    The OS drops it when the process exits (also on a crash or kill), so there is no stale-lock
    cleanup. A filesystem without lock support only logs a warning: the guard is a safety net,
    not a reason to refuse to run.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / LOCK_FILE
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        fh = open(self.path, "a+b")  # noqa: SIM115 - held until release()
        try:
            _lock(fh)
        except OSError as exc:
            fh.close()
            if exc.errno not in _LOCK_HELD:
                log.warning("cannot lock %s (%s); single-process guard is off", self.path, exc)
                return
            raise DataDirInUse(
                f"another ASR server is already running on ASR_DATA_DIR {self.path.parent} "
                f"(lock file {self.path}). The backend must run as exactly one process: stop "
                "the other one, or give this one its own ASR_DATA_DIR."
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n".encode())
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        with contextlib.suppress(OSError):  # closing drops the lock anyway
            _unlock(fh)
        fh.close()


if os.name == "nt":
    import msvcrt

    def _lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def ensure_log_redaction(env: EnvSettings, *, install: bool = False) -> None:
    """Redact secrets from every log line however the app was launched (AGENTS rule 3).

    ``asr serve`` installs the server handlers (with ``SecretRedactingFilter``) before uvicorn
    starts. ``uvicorn server.app:app`` does not: there the root logger has no handlers, so with
    ``install`` (the lifespan, i.e. only when actually serving) they are installed here. Either
    way uvicorn's own handlers, any other root handler and Python's last-resort handler get the
    filter. Idempotent.
    """
    root = logging.getLogger()
    if install and not root.handlers:
        configure_logging(env.log_level, env.log_format, env.log_file)
    handlers = list(root.handlers)
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    for name in _UVICORN_LOGGERS:
        handlers.extend(logging.getLogger(name).handlers)
    for handler in handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())


def create_app(env: EnvSettings | None = None) -> FastAPI:
    env = env or get_env()
    ensure_log_redaction(env)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The core library reads ASR_DECODER / ASR_TORCH_THREADS / ASR_LOCAL_DTYPE from the
        # process env; keep them in sync with values that came from a .env file.
        apply_core_environ(env)
        ensure_log_redaction(env, install=True)
        ensure_dirs(env)
        lock = DataDirLock(env.data_dir)
        try:
            lock.acquire()
        except DataDirInUse as exc:
            log.error("%s", exc)
            raise
        try:
            db = await Database.open(env.db_path)
            job_manager = JobManager(db=db, env=env)
            app.state.db = db
            app.state.job_manager = job_manager
            warm_up(app.state.hardware, env)
            try:
                await job_manager.start()
                yield
            finally:
                await job_manager.stop()
                await db.close()
        finally:
            lock.release()

    app = FastAPI(
        title="Multi-Speaker ASR",
        version=__version__,
        description="Speaker-labelled, timestamped transcription (local and cloud providers).",
        lifespan=lifespan,
    )
    app.state.env = env
    app.state.model_downloads = ModelDownloads(env.model_cache_dir)
    app.state.hardware = HardwareCache()

    for router in ROUTERS:
        app.include_router(router)

    # Order matters: the last middleware added is the outermost. CORS wraps auth so that 401
    # responses carry CORS headers and preflight requests are answered before auth runs; the
    # 2 MB JSON body cap sits inside auth.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_JSON_BODY)
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=env.api_key,
        allowed_hosts=env.allowed_hosts,
        allowed_origins=env.cors_origins,
        allow_network=env.allow_unauthenticated_network,
    )
    if not env.api_key_required:
        hosts = f" and {', '.join(env.allowed_hosts)}" if env.allowed_hosts else ""
        if env.allow_unauthenticated_network:
            log.warning(
                "ASR_API_KEY is empty: API authentication is DISABLED and "
                "ASR_ALLOW_UNAUTHENTICATED_NETWORK accepts clients from any address (Host names: "
                "loopback%s). Anyone who can reach this port controls the API; publish it on "
                "127.0.0.1 only, or set ASR_API_KEY.",
                hosts,
            )
        else:
            log.warning(
                "ASR_API_KEY is empty: API authentication is DISABLED. Only clients connecting "
                "from this machine (loopback) with a loopback Host name%s and no proxy headers "
                "are accepted. A port forwarder or proxy on this machine (netsh portproxy, socat, "
                "ssh -R, nginx without X-Forwarded-For) makes every client it serves look local: "
                "set ASR_API_KEY before putting anything in front of this port.",
                hosts,
            )
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
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


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422 without ``input`` / ``ctx``: the default echoes submitted values back, which
    would return a rejected DashScope key to the browser (AGENTS.md rule 3)."""
    raw = exc.errors() if isinstance(exc, RequestValidationError) else []
    errors = [{k: v for k, v in err.items() if k not in ("input", "ctx")} for err in raw]
    return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)


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
