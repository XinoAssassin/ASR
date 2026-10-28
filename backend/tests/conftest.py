"""Shared fixtures. Frozen contract.

- ``env_factory(**env_vars)``: hermetic ``EnvSettings`` (no ``.env`` files, every ASR/DashScope
  variable cleared, tmp data dir / upload root / model dir, CORS origin ``ALLOWED_ORIGIN``), with
  extra variables applied on top, e.g. ``env_factory(ASR_API_KEY="k")``. The variables are also
  set in ``os.environ`` and ``config.get_env`` is cache-cleared, so ``get_env()`` agrees.
- ``env``: ``env_factory()`` (no API key). ``api_key_env``: same with ``ASR_API_KEY=API_KEY``.
- ``app``: ``create_app(env)`` with its lifespan running (DB open, dirs created).
- ``client``: ``httpx.AsyncClient`` on that app, ``base_url="http://test"``.
- ``make_client(env, headers=None)``: async context manager for any other env, e.g.
  ``async with make_client(api_key_env, headers={"X-API-Key": API_KEY}) as c: ...``.

Override ``env`` in a test module to change the default app, e.g.
``@pytest.fixture
def env(api_key_env): return api_key_env``.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from server import config
from server.app import create_app
from server.config import EnvSettings

API_KEY = "test-api-key"
ALLOWED_ORIGIN = "http://allowed.test"

# Every variable EnvSettings reads; cleared so the developer's shell cannot leak into tests.
ENV_VARS: tuple[str, ...] = tuple(
    str(field.validation_alias) for field in EnvSettings.model_fields.values()
)

EnvFactory = Callable[..., EnvSettings]
ClientFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def env_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[EnvFactory]:
    monkeypatch.setattr(config, "ENV_FILES", ())

    def build(**env_vars: str) -> EnvSettings:
        for name in ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        defaults = {
            "ASR_DATA_DIR": str(tmp_path / "data"),
            "ASR_UPLOAD_ROOTS": str(tmp_path / "uploads"),
            "ASR_MODEL_CACHE_DIR": str(tmp_path / "models"),
            "ASR_CORS_ORIGINS": ALLOWED_ORIGIN,
            "ASR_ALLOWED_HOSTS": "test",  # httpx base_url host; auth is off by default
        }
        for name, value in {**defaults, **env_vars}.items():
            monkeypatch.setenv(name, value)
        config.get_env.cache_clear()
        return config.get_env()

    yield build
    config.get_env.cache_clear()


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory()


@pytest.fixture
def api_key_env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(ASR_API_KEY=API_KEY)


@asynccontextmanager
async def _running_app(env: EnvSettings) -> AsyncIterator[FastAPI]:
    app = create_app(env)
    async with LifespanManager(app):
        yield app


@asynccontextmanager
async def _client_for(
    env: EnvSettings, headers: dict[str, str] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        _running_app(env) as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        yield client


@pytest.fixture
async def app(env: EnvSettings) -> AsyncIterator[FastAPI]:
    async with _running_app(env) as running:
        yield running


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def make_client() -> ClientFactory:
    return _client_for
