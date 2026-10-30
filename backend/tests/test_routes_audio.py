"""``GET /api/audio/{upload_id}``: cached MP3 with Range support (EXECUTION_LOG D12)."""

# Blocking filesystem checks inside async tests are fine (tmp dirs, no event-loop load).
# ruff: noqa: ASYNC240

from __future__ import annotations

import asyncio
import io
import os
import time
from pathlib import Path

import av
import httpx
import pytest

from asr.audio import transcode
from server.config import EnvSettings
from server.routes import audio as audio_route
from tests.conftest import API_KEY, ClientFactory
from tests.test_audio_fixtures import correlated_stereo, write_audio


async def _upload(client: httpx.AsyncClient, data: bytes, name: str = "a.m4a") -> dict:
    r = await client.post("/api/uploads", files={"file": (name, data)}, data={"target_dir": ""})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def stereo_m4a(tmp_path: Path) -> bytes:
    return write_audio(tmp_path, "m4a", samples=correlated_stereo(3.0, 48_000)).read_bytes()


async def test_full_and_range(
    client: httpx.AsyncClient, env: EnvSettings, stereo_m4a: bytes
) -> None:
    upload = await _upload(client, stereo_m4a)
    r = await client.get(f"/api/audio/{upload['id']}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers.get("accept-ranges") == "bytes"
    full = r.content
    with av.open(io.BytesIO(full)) as c:
        s = c.streams.audio[0]
        assert (c.format.name, s.rate, s.layout.nb_channels) == ("mp3", 48_000, 2)
    assert (env.data_dir / "cache" / "audio" / f"{upload['id']}.mp3").is_file()

    part = await client.get(f"/api/audio/{upload['id']}", headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-1023/{len(full)}"
    assert part.content == full[:1024]
    tail = await client.get(f"/api/audio/{upload['id']}", headers={"Range": "bytes=-100"})
    assert tail.status_code == 206
    assert tail.content == full[-100:]


async def test_transcodes_once_under_concurrency(
    client: httpx.AsyncClient, stereo_m4a: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = await _upload(client, stereo_m4a)
    calls: list[Path] = []
    real = transcode.transcode_for_browser

    def counting(src: Path, dst: Path, *, stream_index: int = 0) -> Path:
        calls.append(src)
        return real(src, dst, stream_index=stream_index)

    monkeypatch.setattr(audio_route, "transcode_for_browser", counting)
    results = await asyncio.gather(*(client.get(f"/api/audio/{upload['id']}") for _ in range(5)))
    assert [r.status_code for r in results] == [200] * 5
    assert len({r.content for r in results}) == 1
    assert len(calls) == 1


async def test_cache_refreshed_when_source_changes(
    client: httpx.AsyncClient, tmp_path: Path, stereo_m4a: bytes
) -> None:
    upload = await _upload(client, stereo_m4a)
    first = (await client.get(f"/api/audio/{upload['id']}")).content
    src = Path(upload["path"])
    mono = write_audio(tmp_path, "m4a", channels=1, seconds=1.0, name="mono.m4a").read_bytes()
    src.write_bytes(mono)
    future = time.time() + 5
    os.utime(src, (future, future))
    second = (await client.get(f"/api/audio/{upload['id']}")).content
    assert second != first


async def test_unknown_and_missing(client: httpx.AsyncClient, stereo_m4a: bytes) -> None:
    assert (await client.get("/api/audio/nope")).status_code == 404
    assert (await client.get("/api/audio/..%2F..%2Fetc")).status_code == 404
    upload = await _upload(client, stereo_m4a)
    Path(upload["path"]).unlink()
    assert (await client.get(f"/api/audio/{upload['id']}")).status_code == 404


async def test_api_key_enforced(
    api_key_env: EnvSettings, make_client: ClientFactory, stereo_m4a: bytes
) -> None:
    async with make_client(api_key_env, headers={"X-API-Key": API_KEY}) as authed:
        upload = await _upload(authed, stereo_m4a)
        assert (await authed.get(f"/api/audio/{upload['id']}")).status_code == 200
        wrong = await authed.get(f"/api/audio/{upload['id']}", headers={"X-API-Key": "wrong"})
        assert wrong.status_code == 401
    async with make_client(api_key_env) as anon:
        assert (await anon.get(f"/api/audio/{upload['id']}")).status_code == 401
