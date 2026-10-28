"""Regression tests for the Phase 4 security review (docs/EXECUTION_LOG.md, decisions D24-D28)."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from server import storage
from server.config import EnvSettings
from tests.conftest import ALLOWED_ORIGIN, API_KEY, ClientFactory, EnvFactory
from tests.test_audio_fixtures import wav_bytes
from tests.test_jobs import FakeProvider, install, wait_status

SECRET = "sk-SECRETVALUE0123456789"


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    # Job creation needs a configured DashScope key; the fake provider never uses it.
    return env_factory(DASHSCOPE_API_KEY="sk-test-not-used-0000")


# ------------------------------------------------------------------ open mode (no ASR_API_KEY)
async def test_open_mode_rejects_foreign_host(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/settings", headers={"Host": "rebind.evil.example"})
    assert r.status_code == 403
    for host in ("localhost:8000", "127.0.0.1", "[::1]:8000", "test"):
        assert (await client.get("/api/settings", headers={"Host": host})).status_code == 200


async def test_open_mode_rejects_cross_site_writes(client: httpx.AsyncClient) -> None:
    files = {"file": ("csrf.wav", wav_bytes(0.2))}
    r = await client.post(
        "/api/uploads",
        data={"target_dir": ""},
        files=files,
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    r = await client.post(
        "/api/uploads", data={"target_dir": ""}, files=files, headers={"Origin": ALLOWED_ORIGIN}
    )
    assert r.status_code == 201
    # Loopback origins (the same SPA opened as 127.0.0.1, another dev port) are local.
    files = {"file": ("local.wav", wav_bytes(0.2))}
    for origin in ("http://127.0.0.1:5173", "http://[::1]:8080", "http://localhost:4000"):
        r = await client.post(
            "/api/uploads", data={"target_dir": ""}, files=files, headers={"Origin": origin}
        )
        assert r.status_code == 201, origin
    r = await client.post(
        "/api/uploads",
        data={"target_dir": ""},
        files=files,
        headers={"Origin": "http://localhost.evil.example"},
    )
    assert r.status_code == 403
    # Safe methods are left to CORS (the browser cannot read the response).
    r = await client.get("/api/fs/roots", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


async def test_allowed_hosts_env(env_factory: EnvFactory, make_client: ClientFactory) -> None:
    async with make_client(env_factory(ASR_ALLOWED_HOSTS="asr.lan, test")) as c:
        assert (await c.get("/api/settings", headers={"Host": "ASR.lan:8000"})).status_code == 200
        assert (await c.get("/api/settings", headers={"Host": "other.lan"})).status_code == 403


async def test_host_not_checked_when_key_required(
    api_key_env: EnvSettings, make_client: ClientFactory
) -> None:
    async with make_client(api_key_env, headers={"X-API-Key": API_KEY}) as c:
        r = await c.get("/api/settings", headers={"Host": "asr.example.com"})
        assert r.status_code == 200


# ------------------------------------------------------------------ validation errors (L1)
async def test_validation_errors_do_not_echo_secrets(client: httpx.AsyncClient) -> None:
    too_long = SECRET + "A" * 600
    r = await client.put("/api/settings", json={"cloud": {"api_key": too_long}})
    assert r.status_code == 422
    assert SECRET not in r.text
    assert r.json()["detail"][0]["loc"][-1] == "api_key"

    r = await client.put("/api/settings", json=[{"cloud": {"api_key": SECRET}}])
    assert r.status_code == 422
    assert SECRET not in r.text


# ------------------------------------------------------------ stored path re-validation (L2)
async def _register_then_swap(client: httpx.AsyncClient, env: EnvSettings) -> tuple[str, Path]:
    root = env.upload_roots[0]
    mine = root / "mine.wav"
    mine.write_bytes(wav_bytes(0.5))
    r = await client.post("/api/uploads/register", json={"path": "mine.wav"})
    assert r.status_code == 201, r.text
    outside = env.data_dir.parent / "outside" / "private.wav"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(wav_bytes(1.0))
    mine.unlink()
    try:
        mine.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted here")
    return r.json()["id"], outside


async def test_audio_refuses_swapped_symlink(client: httpx.AsyncClient, env: EnvSettings) -> None:
    upload_id, _ = await _register_then_swap(client, env)
    r = await client.get(f"/api/audio/{upload_id}")
    assert r.status_code == 403
    assert not (env.data_dir / "cache" / "audio" / f"{upload_id}.mp3").exists()


async def test_job_refuses_swapped_symlink(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    provider = FakeProvider()
    install(app, provider)
    upload_id, _ = await _register_then_swap(client, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload_id})).json()
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "failed"
    assert "outside the upload roots" in done["error"]


def test_resolve_stored_file_returns_realpath(env: EnvSettings) -> None:
    root = env.upload_roots[0]
    root.mkdir(parents=True, exist_ok=True)
    (root / "sub").mkdir()
    f = root / "sub" / "a.wav"
    f.write_bytes(b"x")
    assert storage.resolve_stored_file(str(root / "sub" / ".." / "sub" / "a.wav"), [root]) == Path(
        os.path.realpath(f)
    )
    with pytest.raises(storage.NotFoundError):
        storage.resolve_stored_file(str(root / "missing.wav"), [root])


# ------------------------------------------------------------------ upload resource limits (L3)
async def test_chunked_session_cap(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "MAX_OPEN_SESSIONS", 2)
    body = {"filename": "a.wav", "size": 10, "target_dir": ""}
    for _ in range(2):
        assert (await client.post("/api/uploads/init", json=body)).status_code == 201
    r = await client.post("/api/uploads/init", json=body)
    assert r.status_code == 429
    # Abandoned sessions stop counting once idle, so they cannot lock out new uploads.
    old = time.time() - storage.ACTIVE_SESSION_S - 60
    for d in storage.sessions_dir(env.data_dir).iterdir():
        os.utime(d, (old, old))
    assert (await client.post("/api/uploads/init", json=body)).status_code == 201


async def test_chunked_init_checks_free_space(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = shutil.disk_usage

    def tiny(path: str | os.PathLike[str]) -> object:
        usage = real(path)
        return type(usage)(usage.total, usage.total - 1024, 1024)

    monkeypatch.setattr(storage.shutil, "disk_usage", tiny)
    r = await client.post(
        "/api/uploads/init", json={"filename": "a.wav", "size": 10_000, "target_dir": ""}
    )
    assert r.status_code == 507


# ------------------------------------------------------------- data dir vs upload roots (L4)
def test_config_errors_do_not_leak_secrets(env_factory: EnvFactory, tmp_path: Path) -> None:
    secrets = ("sk-0123456789abcdef0123456789abcdef", "my-very-secret-asr-key-XYZ")
    with pytest.raises(ValueError) as info:
        env_factory(
            ASR_DATA_DIR=str(tmp_path / "data"),
            ASR_UPLOAD_ROOTS=str(tmp_path),
            DASHSCOPE_API_KEY=secrets[0],
            ASR_API_KEY=secrets[1],
        )
    text = str(info.value) + repr(info.value)
    assert "contains ASR_DATA_DIR" in text
    assert info.value.__cause__ is None and info.value.__suppress_context__
    for secret in secrets:
        assert secret[4:12] not in text


@pytest.mark.parametrize("root", ["data", ".", "data/uploads"])
def test_root_must_not_contain_data_dir(env_factory: EnvFactory, tmp_path: Path, root: str) -> None:
    roots = str(tmp_path / root)
    if root == "data/uploads":  # a root below the data dir is the default layout
        assert env_factory(ASR_DATA_DIR=str(tmp_path / "data"), ASR_UPLOAD_ROOTS=roots)
        return
    with pytest.raises(ValueError, match="contains ASR_DATA_DIR"):
        env_factory(ASR_DATA_DIR=str(tmp_path / "data"), ASR_UPLOAD_ROOTS=roots)
