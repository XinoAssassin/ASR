"""Settings overlay, masking, endpoint allow-list, api_key write-only semantics, test-cloud."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from asr.providers import cloud as cloud_provider
from asr.providers.base import ConnectionTestResult
from server.config import EnvSettings, effective_settings, mask_secret
from tests.conftest import ClientFactory, EnvFactory

ENV_KEY = "sk-envkey-abcdefgh1111"
NEW_KEY = "sk-test-1234567890abcd"


@pytest.fixture(autouse=True)
def fake_cloud(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand-ins for the cloud provider helpers (no network)."""
    calls: list[dict[str, Any]] = []

    def is_allowed(url: str) -> bool:
        return url in cloud_provider.ALLOWED_ENDPOINTS or url.endswith(
            ".cn-beijing.maas.aliyuncs.com"
        )

    def test_connection(**kwargs: Any) -> ConnectionTestResult:
        calls.append(kwargs)
        return ConnectionTestResult(ok=True, message="ok", max_file_size_mb=2048)

    monkeypatch.setattr(cloud_provider, "is_allowed_endpoint", is_allowed)
    monkeypatch.setattr(cloud_provider, "test_connection", test_connection)
    return calls


def test_mask_secret() -> None:
    assert mask_secret(None) is None
    assert mask_secret("") is None
    assert mask_secret(SecretStr("")) is None
    assert mask_secret(SecretStr(NEW_KEY)) == "sk-****abcd"
    assert mask_secret("abcdefghijklmnop") == "****mnop"
    assert mask_secret("short") == "****"


def test_effective_settings_overlay(env: EnvSettings) -> None:
    eff = effective_settings(env, {})
    assert eff.provider_default == "cloud" and eff.cloud_model == env.cloud_model
    eff = effective_settings(
        env,
        {
            "cloud.model": "m2",
            "cloud.api_key": NEW_KEY,
            "provider_default": "bogus",  # invalid stored values are ignored
            "local.device": "cuda",
        },
    )
    assert eff.cloud_model == "m2"
    assert eff.cloud_api_key.get_secret_value() == NEW_KEY
    assert eff.provider_default == "cloud"
    assert eff.local_device == "cuda"
    assert NEW_KEY not in repr(eff) and NEW_KEY not in str(eff)


async def test_get_defaults(client: httpx.AsyncClient, env: EnvSettings) -> None:
    body = (await client.get("/api/settings")).json()
    # Env-only facts (ARCH_REVIEW §4-f) are covered in test_server_hardening.
    environment, env_only = body.pop("environment"), body.pop("env_only")
    assert environment["upload_roots"] == [str(r) for r in env.upload_roots]
    assert env_only["upload_roots"] == "ASR_UPLOAD_ROOTS"
    assert body == {
        "provider_default": "cloud",
        "api_key_required": False,
        "upload_roots": [str(r) for r in env.upload_roots],
        "local": {
            "asr_model": None,
            "diarize_model": None,
            "device": "cpu",
            "model_cache_dir": str(env.model_cache_dir),
        },
        "cloud": {
            "endpoint": "https://dashscope.aliyuncs.com",
            "model": "qwen-audio-3.1-asr-flash-filetrans",
            "file_transfer": "temp_upload",
            "api_key_set": False,
            "api_key_masked": None,
            "api_key_source": None,
        },
        "llm": {
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com",
            "model": "qwen-plus",
            "api_key_set": False,
            "api_key_masked": None,
        },
        "sources": dict.fromkeys(
            (
                "cloud.endpoint",
                "cloud.model",
                "cloud.file_transfer",
                "cloud.api_key",
                "llm.provider",
                "llm.endpoint",
                "llm.model",
                "llm.api_key",
            ),
            "default",
        ),
        "config_file": {"path": None, "loaded": False, "error": None, "warnings": []},
    }


async def test_api_key_set_and_clear(env_factory: EnvFactory, make_client: ClientFactory) -> None:
    async with make_client(env_factory(DASHSCOPE_API_KEY=ENV_KEY)) as c:
        body = (await c.get("/api/settings")).json()
        assert body["cloud"]["api_key_set"] is True
        assert body["cloud"]["api_key_masked"] == "sk-****1111"

        resp = await c.put("/api/settings", json={"cloud": {"api_key": NEW_KEY}})
        assert resp.status_code == 200
        assert NEW_KEY not in resp.text
        assert resp.json()["cloud"]["api_key_masked"] == "sk-****abcd"

        # Omitted leaves it.
        resp = await c.put("/api/settings", json={"cloud": {"model": "other-model"}})
        assert resp.json()["cloud"]["api_key_masked"] == "sk-****abcd"
        assert resp.json()["cloud"]["model"] == "other-model"

        # "" clears the override -> env value applies again.
        resp = await c.put("/api/settings", json={"cloud": {"api_key": ""}})
        assert resp.json()["cloud"]["api_key_masked"] == "sk-****1111"

        for path in ("/api/settings", "/openapi.json"):
            text = (await c.get(path)).text
            assert NEW_KEY not in text and ENV_KEY not in text


async def test_api_key_clear_without_env(client: httpx.AsyncClient) -> None:
    await client.put("/api/settings", json={"cloud": {"api_key": NEW_KEY}})
    assert (await client.get("/api/settings")).json()["cloud"]["api_key_set"] is True
    body = (await client.put("/api/settings", json={"cloud": {"api_key": ""}})).json()
    assert body["cloud"] == {**body["cloud"], "api_key_set": False, "api_key_masked": None}
    bad = await client.put("/api/settings", json={"cloud": {"api_key": "sk-a b"}})
    assert bad.status_code == 422
    assert "sk-a b" not in bad.text


async def test_partial_update(client: httpx.AsyncClient) -> None:
    resp = await client.put(
        "/api/settings",
        json={
            "provider_default": "local",
            "local": {"asr_model": "qwen3-asr-0.6b", "device": "cuda"},
            "cloud": {
                "endpoint": "https://cn-beijing.maas.qianwenaiapi.com/",
                "file_transfer": "public_url",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider_default"] == "local"
    assert body["local"]["asr_model"] == "qwen3-asr-0.6b"
    assert body["local"]["device"] == "cuda"
    assert body["local"]["diarize_model"] is None
    assert body["cloud"]["endpoint"] == "https://cn-beijing.maas.qianwenaiapi.com"
    assert body["cloud"]["file_transfer"] == "public_url"
    assert (await client.get("/api/settings")).json() == body


@pytest.mark.parametrize(
    "body",
    [
        {"cloud": {"endpoint": "https://evil.example.com"}},
        {"cloud": {"endpoint": "http://dashscope.aliyuncs.com"}},
        {"cloud": {"file_transfer": "oss"}},
        {"cloud": {"model": "x" * 201}},
        {"provider_default": "other"},
        {"local": {"device": "tpu"}},
    ],
)
async def test_rejected_updates(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    before = (await client.get("/api/settings")).json()
    assert (await client.put("/api/settings", json=body)).status_code == 422
    assert (await client.get("/api/settings")).json() == before


async def test_env_only_fields_ignored(client: httpx.AsyncClient, env: EnvSettings) -> None:
    resp = await client.put(
        "/api/settings", json={"api_key_required": True, "upload_roots": ["/etc"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key_required"] is False
    assert body["upload_roots"] == [str(r) for r in env.upload_roots]


async def test_test_cloud_without_key_makes_no_call(
    client: httpx.AsyncClient, fake_cloud: list[dict[str, Any]]
) -> None:
    body = (await client.post("/api/settings/test-cloud")).json()
    assert body["ok"] is False and "not configured" in body["message"]
    assert fake_cloud == []


async def test_test_cloud_uses_effective_settings(
    client: httpx.AsyncClient, fake_cloud: list[dict[str, Any]]
) -> None:
    await client.put("/api/settings", json={"cloud": {"api_key": NEW_KEY, "model": "m"}})
    resp = await client.post("/api/settings/test-cloud")
    assert resp.json() == {"ok": True, "message": "ok", "max_file_size_mb": 2048}
    assert fake_cloud == [
        {"api_key": NEW_KEY, "endpoint": "https://dashscope.aliyuncs.com", "model": "m"}
    ]
    assert NEW_KEY not in resp.text


async def test_test_cloud_exception_is_generic(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(**kwargs: Any) -> ConnectionTestResult:
        raise RuntimeError(f"leak {kwargs['api_key']}")

    monkeypatch.setattr(cloud_provider, "test_connection", boom)
    await client.put("/api/settings", json={"cloud": {"api_key": NEW_KEY}})
    resp = await client.post("/api/settings/test-cloud")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert NEW_KEY not in resp.text and NEW_KEY not in caplog.text


async def test_local_models(client: httpx.AsyncClient) -> None:
    models = (await client.get("/api/settings/local-models")).json()
    assert models and all(m["downloaded"] is False for m in models)
    assert {m["kind"] for m in models} == {"asr", "aligner", "vad", "diarization"}


async def test_unknown_local_asr_model_rejected(client: httpx.AsyncClient) -> None:
    for bad in ("Qwen/Qwen3-ASR-0.6B", "campplus", "whisper"):
        resp = await client.put("/api/settings", json={"local": {"asr_model": bad}})
        assert resp.status_code == 422, bad
    resp = await client.put("/api/settings", json={"local": {"asr_model": ""}})
    assert resp.status_code == 200  # empty clears the override (M4 default applies)


async def test_local_models_report_download_state(
    client: httpx.AsyncClient, env: EnvSettings
) -> None:
    from asr.providers.local import models

    path = models.model_path(env.model_cache_dir, "fsmn-vad")
    path.mkdir(parents=True)
    (path / "model.pt").write_bytes(b"x" * 2048)
    resp = await client.get("/api/settings/local-models")
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()}
    assert set(rows) == set(models.MODELS)
    assert rows["fsmn-vad"]["downloaded"] is True and rows["fsmn-vad"]["kind"] == "vad"
    assert (
        rows["qwen3-asr-1.7b"]["downloaded"] is False and rows["qwen3-asr-1.7b"]["size_mb"] is None
    )
    assert rows["qwen3-forced-aligner"]["kind"] == "aligner"
    assert rows["campplus"]["kind"] == "diarization"


async def test_download_local_model(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from asr.providers.local import models

    release = threading.Event()
    calls: list[tuple[str, bool]] = []

    def fake_download(cache: Path, key: str, *, mirror: bool = False) -> Path:
        calls.append((key, mirror))
        release.wait(5)
        path = models.model_path(cache, key)
        path.mkdir(parents=True, exist_ok=True)
        (path / models.spec(key).marker).write_bytes(b"x")
        return path

    monkeypatch.setattr(models, "download", fake_download)
    assert (await client.post("/api/settings/local-models/whisper/download")).status_code == 404
    resp = await client.post("/api/settings/local-models/qwen3-asr-1.7b/download?mirror=true")
    assert resp.status_code == 202 and resp.json()["status"] == "downloading"
    busy = await client.post("/api/settings/local-models/campplus/download")
    assert busy.status_code == 409
    release.set()
    for _ in range(100):
        rows = {r["id"]: r for r in (await client.get("/api/settings/local-models")).json()}
        if rows["qwen3-asr-1.7b"]["status"] == "downloaded":
            break
        await asyncio.sleep(0.02)
    assert rows["qwen3-asr-1.7b"]["status"] == "downloaded" and rows["qwen3-asr-1.7b"]["downloaded"]
    assert rows["qwen3-asr-1.7b"]["has_mirror"] is True
    assert calls == [("qwen3-asr-1.7b", True)]


async def test_failed_download_reports_error(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from asr.providers.local import models

    def broken(cache: Path, key: str, *, mirror: bool = False) -> Path:
        raise RuntimeError("network down")

    monkeypatch.setattr(models, "download", broken)
    assert (await client.post("/api/settings/local-models/campplus/download")).status_code == 202
    for _ in range(100):
        rows = {r["id"]: r for r in (await client.get("/api/settings/local-models")).json()}
        if rows["campplus"]["status"] == "failed":
            break
        await asyncio.sleep(0.02)
    assert rows["campplus"]["status"] == "failed"
    assert "network down" in rows["campplus"]["error"]
