"""Every backend-jobs route requires ``X-API-Key`` when ``ASR_API_KEY`` is set."""

import httpx
import pytest

from server.auth import api_key_matches
from server.config import EnvSettings
from tests.conftest import API_KEY, ClientFactory

ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/jobs"),
    ("GET", "/api/jobs"),
    ("GET", "/api/jobs/x"),
    ("GET", "/api/jobs/x/events"),
    ("POST", "/api/jobs/x/cancel"),
    ("POST", "/api/jobs/x/retry"),
    ("DELETE", "/api/jobs/x"),
    ("GET", "/api/transcripts/x"),
    ("PATCH", "/api/transcripts/x"),
    ("PUT", "/api/transcripts/x/speakers"),
    ("POST", "/api/transcripts/x/revert"),
    ("GET", "/api/transcripts/x/export"),
    ("GET", "/api/term-sets"),
    ("POST", "/api/term-sets"),
    ("POST", "/api/term-sets/import"),
    ("GET", "/api/term-sets/x"),
    ("PUT", "/api/term-sets/x"),
    ("DELETE", "/api/term-sets/x"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("POST", "/api/settings/test-cloud"),
    ("GET", "/api/settings/local-models"),
]


def test_api_key_matches() -> None:
    from pydantic import SecretStr

    assert api_key_matches("k", SecretStr("k"))
    assert not api_key_matches("K", SecretStr("k"))
    assert not api_key_matches(None, SecretStr("k"))


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_routes_require_key(
    api_key_env: EnvSettings, make_client: ClientFactory, method: str, path: str
) -> None:
    async with make_client(api_key_env) as c:
        assert (await c.request(method, path)).status_code == 401
        wrong = await c.request(method, path, headers={"X-API-Key": "wrong"})
        assert wrong.status_code == 401
        assert (await c.request(method, f"{path}?api_key={API_KEY}")).status_code == 401
        ok = await c.request(method, path, headers={"X-API-Key": API_KEY})
        assert ok.status_code != 401


async def test_authorised_requests_work(
    api_key_env: EnvSettings, make_client: ClientFactory
) -> None:
    async with make_client(api_key_env, headers={"X-API-Key": API_KEY}) as c:
        resp = await c.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["api_key_required"] is True
        assert API_KEY not in resp.text
        assert (await c.get("/api/jobs")).status_code == 200


async def test_health_stays_open(api_key_env: EnvSettings, make_client: ClientFactory) -> None:
    async with make_client(api_key_env) as c:
        resp: httpx.Response = await c.get("/api/health")
        assert resp.status_code == 200
