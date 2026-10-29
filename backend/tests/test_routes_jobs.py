"""``/api/jobs`` routes: creation validation, term expansion, list, delete, SSE."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from server.config import EnvSettings
from tests.conftest import ClientFactory, EnvFactory
from tests.test_jobs import (
    DASHSCOPE_KEY,
    FakeProvider,
    add_upload,
    install,
    wait_for,
    wait_status,
)


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


@pytest.fixture
def provider(app: FastAPI) -> FakeProvider:
    p = FakeProvider()
    install(app, p)
    return p


def _is_file(path: str | Path) -> bool:
    return Path(path).is_file()


def _exists(path: str | Path) -> bool:
    return Path(path).exists()


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if not lines:
            continue
        event = {"event": None, "data": ""}
        for ln in lines:
            key, _, value = ln.partition(":")
            value = value.removeprefix(" ")
            if key == "event":
                event["event"] = value
            elif key == "data":
                event["data"] += value
        events.append({"event": event["event"], "data": json.loads(event["data"])})
    return events


# ------------------------------------------------------------------------------------ create
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"upload_id": "x", "file_url": "https://example.com/a.mp3"},
    ],
)
async def test_exactly_one_source(
    client: httpx.AsyncClient, provider: FakeProvider, body: dict[str, Any]
) -> None:
    assert (await client.post("/api/jobs", json=body)).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.mp3",
        "http://user:pw@example.com/a.mp3",
        "https://user@example.com/a.mp3",
        "https:///nohost",
        "javascript:alert(1)",
        "https://example.com/" + "a" * 2100,
    ],
)
async def test_bad_file_urls(client: httpx.AsyncClient, provider: FakeProvider, url: str) -> None:
    resp = await client.post("/api/jobs", json={"file_url": url})
    assert resp.status_code == 422, resp.text


async def test_file_url_job_runs_without_fetching(
    client: httpx.AsyncClient, provider: FakeProvider
) -> None:
    resp = await client.post("/api/jobs", json={"file_url": "https://example.com/会议.mp3"})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["upload_id"] is None and job["provider"] == "cloud"
    await wait_status(client, job["id"], {"succeeded"})
    assert provider.calls[0][0].url == "https://example.com/会议.mp3"


@pytest.mark.parametrize(
    "body",
    [
        {"file_url": "https://example.com/a.mp3", "provider": "local"},
        {"file_url": "https://example.com/a.mp3", "options": {"provider": "local"}},
    ],
)
async def test_local_provider_rejects_file_url(
    client: httpx.AsyncClient, provider: FakeProvider, body: dict[str, Any]
) -> None:
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 422
    assert "file_url" in resp.json()["detail"]


def _fake_models(env: EnvSettings, keys: list[str]) -> None:
    from asr.providers.local import models

    for key in keys:
        path = models.model_path(env.model_cache_dir, key)
        path.mkdir(parents=True, exist_ok=True)
        (path / models.spec(key).marker).write_bytes(b"x")


async def test_local_job_needs_downloaded_models(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, provider: FakeProvider
) -> None:
    upload = await add_upload(app, env)
    body = {
        "upload_id": upload.id,
        "provider": "local",
        "options": {"local": {"asr_model": "qwen3-asr-0.6b"}},
    }
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "asr models download" in detail and "qwen3-asr-0.6b" in detail
    bad = {**body, "options": {"local": {"asr_model": "whisper"}}}
    assert (await client.post("/api/jobs", json=bad)).status_code == 422


async def test_local_job_pins_model_and_needs_no_cloud_key(
    env_factory: EnvFactory, make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = env_factory()  # no DashScope key at all
    _fake_models(env, ["fsmn-vad", "qwen3-asr-0.6b", "qwen3-forced-aligner", "campplus"])
    from server.routes import jobs as jobs_routes

    monkeypatch.setattr(jobs_routes, "default_asr_model", lambda device="cpu": "qwen3-asr-0.6b")
    async with make_client(env) as c:
        app = c._transport.app  # type: ignore[attr-defined]
        install(app, FakeProvider())
        upload = await add_upload(app, env)
        resp = await c.post("/api/jobs", json={"upload_id": upload.id, "provider": "local"})
        assert resp.status_code == 201, resp.text
        job = resp.json()
        assert job["options"]["local"] == {
            "asr_model": "qwen3-asr-0.6b",
            "diarize_model": None,
            "device": "cpu",
        }
        done = await wait_status(c, job["id"], {"succeeded", "failed"})
        assert done["status"] == "succeeded", done


async def test_provider_precedence(client: httpx.AsyncClient, provider: FakeProvider) -> None:
    # JobCreate.provider beats options.provider (D17).
    resp = await client.post(
        "/api/jobs",
        json={
            "file_url": "https://example.com/a.mp3",
            "provider": "cloud",
            "options": {"provider": "local"},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["options"]["provider"] == "cloud"


async def test_settings_default_provider_local_rejected(
    client: httpx.AsyncClient, provider: FakeProvider
) -> None:
    assert (
        await client.put("/api/settings", json={"provider_default": "local"})
    ).status_code == 200
    resp = await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})
    assert resp.status_code == 422


async def test_missing_dashscope_key_rejected(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    async with make_client(env_factory()) as c:
        resp = await c.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})
        assert resp.status_code == 422
        assert "API key" in resp.json()["detail"]


async def test_unknown_upload_and_term_set(
    client: httpx.AsyncClient, provider: FakeProvider
) -> None:
    assert (await client.post("/api/jobs", json={"upload_id": "nope"})).status_code == 404
    resp = await client.post(
        "/api/jobs",
        json={"file_url": "https://example.com/a.mp3", "term_set_ids": ["missing"]},
    )
    assert resp.status_code == 404


async def test_term_expansion_and_dedupe(client: httpx.AsyncClient, provider: FakeProvider) -> None:
    ts1 = (
        await client.post(
            "/api/term-sets",
            json={"name": "chips", "terms": [{"term": "FPGA", "weight": 2}, {"term": "SoC"}]},
        )
    ).json()
    ts2 = (
        await client.post(
            "/api/term-sets",
            json={"name": "more", "terms": [{"term": "fpga", "weight": 4, "note": "n"}]},
        )
    ).json()
    resp = await client.post(
        "/api/jobs",
        json={
            "file_url": "https://example.com/a.mp3",
            "term_set_ids": [ts1["id"], ts2["id"]],
            "extra_terms": [{"term": "Kubernetes", "weight": 50}, {"term": "SOC", "weight": 1}],
            # Client-supplied terms are ignored; the server rebuilds them.
            "options": {"terms": [{"term": "ignored"}]},
        },
    )
    assert resp.status_code == 201, resp.text
    terms = resp.json()["options"]["terms"]
    assert terms == [
        {"term": "FPGA", "weight": 4, "note": "n"},
        {"term": "SoC", "weight": 3, "note": None},
        {"term": "Kubernetes", "weight": 50, "note": None},
    ]
    stored = (await client.get(f"/api/jobs/{resp.json()['id']}")).json()
    assert stored["options"]["terms"] == terms


async def test_term_cap(client: httpx.AsyncClient, provider: FakeProvider) -> None:
    extra = [{"term": f"term{i}"} for i in range(501)]
    resp = await client.post(
        "/api/jobs", json={"file_url": "https://example.com/a.mp3", "extra_terms": extra}
    )
    assert resp.status_code == 422


# -------------------------------------------------------------------------------------- list
async def test_list_filter_and_pagination(
    client: httpx.AsyncClient, provider: FakeProvider
) -> None:
    ids = []
    for _ in range(3):
        job = (
            await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})
        ).json()
        ids.append(job["id"])
    for job_id in ids:
        await wait_status(client, job_id, {"succeeded"})

    page1 = (await client.get("/api/jobs?page=1&page_size=2")).json()
    assert page1["total"] == 3 and page1["page"] == 1 and page1["page_size"] == 2
    assert len(page1["items"]) == 2
    page2 = (await client.get("/api/jobs?page=2&page_size=2")).json()
    assert len(page2["items"]) == 1
    seen = {j["id"] for j in page1["items"] + page2["items"]}
    assert seen == set(ids)

    assert (await client.get("/api/jobs?status=succeeded")).json()["total"] == 3
    assert (await client.get("/api/jobs?status=failed")).json()["total"] == 0
    assert (await client.get("/api/jobs?status=bogus")).status_code == 422
    assert (await client.get("/api/jobs/unknown")).status_code == 404


# ------------------------------------------------------------------------------------ delete
async def test_delete_keeps_audio_by_default(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, provider: FakeProvider
) -> None:
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded"})

    assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 204
    assert (await client.get(f"/api/jobs/{job['id']}")).status_code == 404
    assert (await client.get(f"/api/transcripts/{done['transcript_id']}")).status_code == 404
    assert _is_file(upload.path)
    assert await app.state.db.get_upload(upload.id) is not None
    assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 404


async def test_delete_audio_only_when_unreferenced(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, provider: FakeProvider
) -> None:
    upload = await add_upload(app, env)
    cache = env.data_dir / "cache" / "audio"
    cache.mkdir(parents=True)
    cached = cache / f"{upload.id}.mp3"
    cached.write_bytes(b"mp3")

    a = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    b = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_status(client, a["id"], {"succeeded"})
    await wait_status(client, b["id"], {"succeeded"})

    # b still references the upload: nothing is deleted.
    assert (await client.delete(f"/api/jobs/{a['id']}?delete_audio=true")).status_code == 204
    assert _is_file(upload.path) and _is_file(cached)
    assert await app.state.db.get_upload(upload.id) is not None

    assert (await client.delete(f"/api/jobs/{b['id']}?delete_audio=true")).status_code == 204
    assert not _exists(upload.path)
    assert not _exists(cached)
    assert await app.state.db.get_upload(upload.id) is None


async def test_delete_audio_outside_roots_keeps_file(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, provider: FakeProvider
) -> None:
    upload = await add_upload(app, env)
    outside = env.data_dir / "outside.m4a"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"x")
    await app.state.db.conn.execute(
        "UPDATE uploads SET path = ? WHERE id = ?", (str(outside), upload.id)
    )
    await app.state.db.conn.commit()
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_status(client, job["id"], {"succeeded", "failed"})
    assert (await client.delete(f"/api/jobs/{job['id']}?delete_audio=true")).status_code == 204
    assert outside.is_file()


async def test_delete_audio_keeps_registered_file(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, provider: FakeProvider
) -> None:
    upload = await add_upload(app, env)
    await app.state.db.conn.execute("UPDATE uploads SET registered = 1 WHERE id = ?", (upload.id,))
    await app.state.db.conn.commit()
    assert (await app.state.db.get_upload(upload.id)).registered is True
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_status(client, job["id"], {"succeeded", "failed"})
    assert (await client.delete(f"/api/jobs/{job['id']}?delete_audio=true")).status_code == 204
    assert _is_file(upload.path)  # the user's own file stays
    assert await app.state.db.get_upload(upload.id) is None


async def test_delete_running_job(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    blocker = FakeProvider(block=True)
    install(app, blocker)
    job = (await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})).json()
    await wait_for(blocker.started.is_set)
    assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 204
    await wait_for(lambda: not app.state.job_manager.is_running(job["id"]))
    assert (await client.get(f"/api/jobs/{job['id']}")).status_code == 404


# --------------------------------------------------------------------------------------- SSE
async def test_sse_terminal_job_sends_state_and_closes(
    client: httpx.AsyncClient, provider: FakeProvider
) -> None:
    job = (await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})).json()
    await wait_status(client, job["id"], {"succeeded"})
    async with client.stream("GET", f"/api/jobs/{job['id']}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = (await resp.aread()).decode("utf-8")
    events = _parse_sse(text)
    assert len(events) == 1
    assert events[0]["event"] == "job"
    assert events[0]["data"]["status"] == "succeeded"


async def test_sse_streams_progress_until_terminal(app: FastAPI, client: httpx.AsyncClient) -> None:
    blocker = FakeProvider(block=True)
    install(app, blocker)
    manager = app.state.job_manager
    job = (await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})).json()
    await wait_for(blocker.started.is_set)

    async def consume() -> str:
        async with client.stream("GET", f"/api/jobs/{job['id']}/events") as resp:
            assert resp.status_code == 200
            chunks = [chunk async for chunk in resp.aiter_text()]
        return "".join(chunks)

    task = asyncio.create_task(consume())
    await wait_for(lambda: manager.subscriber_count(job["id"]) == 1)
    blocker.release.set()
    text = await asyncio.wait_for(task, 10)
    events = _parse_sse(text)
    assert all(e["event"] == "job" for e in events)
    assert events[0]["data"]["status"] == "running"
    assert events[-1]["data"]["status"] == "succeeded"
    stages = [e["data"]["stage"] for e in events]
    assert "finalizing" in stages or "recognizing" in stages
    assert stages[-1] == "done"
    assert manager.subscriber_count(job["id"]) == 0  # unsubscribed


async def test_sse_unknown_job_404(client: httpx.AsyncClient, app: FastAPI) -> None:
    assert (await client.get("/api/jobs/nope/events")).status_code == 404
    assert app.state.job_manager.subscriber_count("nope") == 0


async def test_sse_ends_when_job_deleted(app: FastAPI, client: httpx.AsyncClient) -> None:
    blocker = FakeProvider(block=True)
    install(app, blocker)
    manager = app.state.job_manager
    job = (await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})).json()
    await wait_for(blocker.started.is_set)

    async def consume() -> str:
        async with client.stream("GET", f"/api/jobs/{job['id']}/events") as resp:
            return "".join([c async for c in resp.aiter_text()])

    task = asyncio.create_task(consume())
    await wait_for(lambda: manager.subscriber_count(job["id"]) == 1)
    assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 204
    text = await asyncio.wait_for(task, 10)
    assert _parse_sse(text)[0]["data"]["id"] == job["id"]
    assert manager.subscriber_count(job["id"]) == 0
