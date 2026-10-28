import httpx
import pytest
from pydantic import ValidationError

from asr import __version__
from server.config import EnvSettings
from tests.conftest import ALLOWED_ORIGIN, API_KEY, ClientFactory, EnvFactory


async def test_health_ok(client: httpx.AsyncClient, env: EnvSettings) -> None:
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["api_key_required"] is False
    assert body["decoder"] == "pyav"
    assert body["ffmpeg_version"]


async def test_lifespan_creates_dirs_and_db(client: httpx.AsyncClient, env: EnvSettings) -> None:
    assert env.data_dir.is_dir()
    assert env.db_path.is_file()
    assert all(root.is_dir() and root.is_absolute() for root in env.upload_roots)


async def test_no_key_configured_allows_protected_routes(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/settings")
    assert resp.status_code == 200  # not 401


async def test_api_key_enforced(api_key_env: EnvSettings, make_client: ClientFactory) -> None:
    async with make_client(api_key_env) as c:
        health = await c.get("/api/health")
        assert health.status_code == 200
        assert health.json()["api_key_required"] is True

        assert (await c.get("/api/settings")).status_code == 401
        assert (await c.get("/api/settings", headers={"X-API-Key": "wrong"})).status_code == 401
        # Header only, never the query string (D6).
        assert (await c.get(f"/api/settings?api_key={API_KEY}")).status_code == 401
        assert (await c.get("/api/settings", headers={"X-API-Key": API_KEY})).status_code == 200

        assert (await c.get("/openapi.json")).status_code == 200


async def test_cors_preflight_allowed_origin(
    api_key_env: EnvSettings, make_client: ClientFactory
) -> None:
    async with make_client(api_key_env) as c:
        resp = await c.options(
            "/api/jobs",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key, content-type",
            },
        )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "x-api-key" in allowed
    assert "content-type" in allowed


async def test_cors_preflight_disallowed_origin(client: httpx.AsyncClient) -> None:
    resp = await client.options(
        "/api/jobs",
        headers={"Origin": "http://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in resp.headers


async def test_unauthorized_response_carries_cors_headers(
    api_key_env: EnvSettings, make_client: ClientFactory
) -> None:
    async with make_client(api_key_env) as c:
        resp = await c.get("/api/settings", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_cors_wildcard_rejected(env_factory: EnvFactory) -> None:
    with pytest.raises(ValidationError):
        env_factory(ASR_CORS_ORIGINS="*")
