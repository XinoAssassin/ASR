"""JobManager lifecycle with a fake provider (no network, no models).

Also exports the fakes / helpers used by ``test_routes_jobs`` and ``test_routes_transcripts``.
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from asr.providers.base import (
    AudioSource,
    JobOptions,
    ProgressCallback,
    ProgressEvent,
    ProviderError,
    Segment,
    Transcript,
    make_speakers,
)
from server import jobs as jobs_module
from server.app import create_app
from server.config import EffectiveSettings, EnvSettings
from server.db import Database
from server.jobs import INTERRUPTED, JobManager
from server.schemas import JobOut, UploadOut
from tests.conftest import EnvFactory

DASHSCOPE_KEY = "sk-test-1234567890abcd"


# -------------------------------------------------------------------------------------- fakes
def sample_transcript() -> Transcript:
    segments = [
        Segment(
            id="s0", speaker_id="SPEAKER_00", start=0.0, end=2.5, text="今天我们先过一下测试结果。"
        ),
        Segment(
            id="s1", speaker_id="SPEAKER_01", start=2.6, end=4.0, text="好，我先说 chip 部分。"
        ),
        Segment(id="s2", speaker_id="SPEAKER_01", start=4.1, end=5.0, text="然后是 driver。"),
    ]
    return Transcript(
        provider="cloud",
        duration=5.0,
        language="zh",
        speakers=make_speakers(s.speaker_id for s in segments),
        segments=segments,
        warnings=["sample warning"],
    )


class FakeProvider:
    """Emits the cloud stages; optionally blocks (polling ``progress``) until ``release``."""

    name = "cloud"

    def __init__(
        self,
        *,
        result: Transcript | None = None,
        error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self.result = result or sample_transcript()
        self.error = error
        self.block = block
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: list[tuple[AudioSource, JobOptions]] = []
        self.work_dirs: list[Path] = []

    def transcribe(
        self, source: AudioSource, options: JobOptions, progress: ProgressCallback
    ) -> Transcript:
        self.calls.append((source, options))
        progress(ProgressEvent(stage="preparing", progress=0.05))
        progress(ProgressEvent(stage="uploading", progress=0.2))
        progress(ProgressEvent(stage="cloud_queued", progress=0.3, external_id="task-123"))
        self.started.set()
        if self.block:
            deadline = time.monotonic() + 20
            while not self.release.wait(0.01):
                progress(ProgressEvent(stage="recognizing", progress=0.5))
                if time.monotonic() > deadline:
                    raise ProviderError("fake provider timed out")
        progress(ProgressEvent(stage="recognizing", progress=0.8))
        if self.error is not None:
            raise self.error
        progress(ProgressEvent(stage="finalizing", progress=0.95))
        return self.result


class FakeFactory:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.calls: list[tuple[JobOut, EffectiveSettings, Path]] = []

    def __call__(self, job: JobOut, settings: EffectiveSettings, work_dir: Path) -> FakeProvider:
        self.calls.append((job, settings, work_dir))
        assert work_dir.is_dir()
        self.provider.work_dirs.append(work_dir)
        return self.provider


# ------------------------------------------------------------------------------------ helpers
async def add_upload(
    app: FastAPI, env: EnvSettings, name: str = "会议记录.m4a", *, upload_id: str = "up1"
) -> UploadOut:
    root = env.upload_roots[0]
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{upload_id}-{name}"
    path.write_bytes(b"fake audio")
    upload = UploadOut(
        id=upload_id,
        path=str(path),
        original_name=name,
        size=path.stat().st_size,
        created_at=datetime.now(UTC),
    )
    await app.state.db.insert_upload(upload)
    return upload


async def wait_status(
    client: httpx.AsyncClient, job_id: str, statuses: set[str], within: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + within
    while True:
        body = (await client.get(f"/api/jobs/{job_id}")).json()
        if body["status"] in statuses:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} stuck in {body['status']}: {body}")
        await asyncio.sleep(0.02)


async def wait_for(predicate: Any, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def install(app: FastAPI, provider: FakeProvider) -> FakeFactory:
    factory = FakeFactory(provider)
    app.state.job_manager.provider_factory = factory
    return factory


# ----------------------------------------------------------------------------------- fixtures
@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


# -------------------------------------------------------------------------------------- tests
async def test_job_succeeds_and_stores_transcript(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    provider = FakeProvider()
    factory = install(app, provider)
    upload = await add_upload(app, env)

    resp = await client.post("/api/jobs", json={"upload_id": upload.id})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] == "queued" and job["stage"] == "queued"

    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "succeeded", done
    assert done["stage"] == "done"
    assert done["progress"] == 1.0
    assert done["cloud_task_id"] == "task-123"
    assert done["warnings"] == ["sample warning"]
    assert done["started_at"] and done["finished_at"]
    assert done["error"] is None

    source, options = provider.calls[0]
    assert source.path == Path(upload.path)
    assert options.provider == "cloud"
    _, settings, work_dir = factory.calls[0]
    assert settings.cloud_api_key.get_secret_value() == DASHSCOPE_KEY
    assert work_dir == env.data_dir / "work" / job["id"]
    assert not work_dir.exists()  # cleaned up

    tr = (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()
    assert tr["job_id"] == job["id"]
    assert tr["segments"] == tr["original"]
    assert [s["id"] for s in tr["segments"]] == ["s0", "s1", "s2"]
    assert [s["id"] for s in tr["speakers"]] == ["SPEAKER_00", "SPEAKER_01"]


async def test_provider_error_marks_failed(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    install(app, FakeProvider(error=ProviderError("DashScope rejected the file")))
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "failed"
    assert done["error"] == "DashScope rejected the file"
    assert done["transcript_id"] is None


async def test_unexpected_error_is_generic_and_redacted(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install(app, FakeProvider(error=RuntimeError(f"boom with {DASHSCOPE_KEY}")))
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "failed"
    assert done["error"] == jobs_module.GENERIC_FAILURE
    assert DASHSCOPE_KEY not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_missing_upload_file_fails(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    await asyncio.to_thread(Path(upload.path).unlink)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "failed"
    assert "missing" in done["error"]


async def test_cancel_while_running(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    provider = FakeProvider(block=True)
    install(app, provider)
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_for(provider.started.is_set)
    running = await wait_status(client, job["id"], {"running"})
    assert running["status"] == "running"

    resp = await client.post(f"/api/jobs/{job['id']}/cancel")
    assert resp.status_code == 200
    done = await wait_status(client, job["id"], {"cancelled", "succeeded", "failed"})
    assert done["status"] == "cancelled"
    assert done["transcript_id"] is None
    assert done["finished_at"]
    assert not app.state.job_manager.is_running(job["id"])

    # Terminal jobs cannot be cancelled again.
    assert (await client.post(f"/api/jobs/{job['id']}/cancel")).status_code == 409


async def test_retry_right_after_cancel_runs_again(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    """The terminal status is only visible once the run is released, so an immediate retry is
    never dropped as a duplicate of the finishing run."""
    blocker = FakeProvider(block=True)
    install(app, blocker)
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_for(blocker.started.is_set)
    await client.post(f"/api/jobs/{job['id']}/cancel")
    await wait_status(client, job["id"], {"cancelled"})
    assert not app.state.job_manager.is_running(job["id"])

    install(app, FakeProvider())
    assert (await client.post(f"/api/jobs/{job['id']}/retry")).status_code == 200
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "succeeded"


async def test_cancel_while_queued_then_retry(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    manager: JobManager = app.state.job_manager
    blocker = FakeProvider(block=True)
    install(app, blocker)
    manager_concurrency = manager.cloud_concurrency
    upload = await add_upload(app, env)

    # Fill every cloud worker so the next job stays queued.
    busy = [
        (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
        for _ in range(manager_concurrency)
    ]
    for b in busy:
        await wait_status(client, b["id"], {"running"})
    queued = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await asyncio.sleep(0.05)
    assert (await client.get(f"/api/jobs/{queued['id']}")).json()["status"] == "queued"

    resp = await client.post(f"/api/jobs/{queued['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    blocker.release.set()
    for b in busy:
        assert (await wait_status(client, b["id"], {"succeeded"}))["status"] == "succeeded"
    # The stale queue entry is skipped: the job stays cancelled.
    await asyncio.sleep(0.05)
    assert (await client.get(f"/api/jobs/{queued['id']}")).json()["status"] == "cancelled"

    # Retry re-queues and runs it.
    install(app, FakeProvider())
    retried = await client.post(f"/api/jobs/{queued['id']}/retry")
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "queued" and body["progress"] == 0 and body["finished_at"] is None
    done = await wait_status(client, queued["id"], {"succeeded", "failed"})
    assert done["status"] == "succeeded"

    # Only failed / cancelled jobs can be retried.
    assert (await client.post(f"/api/jobs/{queued['id']}/retry")).status_code == 409


async def test_retry_failed_job_resets_fields(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    install(app, FakeProvider(error=ProviderError("nope")))
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    failed = await wait_status(client, job["id"], {"failed"})
    assert failed["cloud_task_id"] == "task-123"

    install(app, FakeProvider())
    body = (await client.post(f"/api/jobs/{job['id']}/retry")).json()
    assert body["error"] is None and body["cloud_task_id"] is None and body["stage"] == "queued"
    assert (await wait_status(client, job["id"], {"succeeded"}))["status"] == "succeeded"


async def test_progress_writes_are_throttled(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    db: Database = app.state.db
    writes: list[dict[str, Any]] = []
    original = db.transition_job

    async def spy(job_id: str, from_statuses: Any, **fields: Any) -> Any:
        writes.append(fields)
        return await original(job_id, from_statuses, **fields)

    db.transition_job = spy  # type: ignore[method-assign]
    provider = FakeProvider(block=True)
    install(app, provider)
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_for(provider.started.is_set)
    await asyncio.sleep(0.6)  # ~60 progress calls from the provider thread
    provider.release.set()
    await wait_status(client, job["id"], {"succeeded"})
    progress_writes = [w for w in writes if "stage" in w and "status" not in w]
    # 4 stage changes + at most a few interval flushes, far fewer than ~60 calls.
    assert 3 <= len(progress_writes) <= 10, progress_writes


async def test_startup_recovery(env: EnvSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """``running`` -> failed (D13); ``queued`` is re-enqueued and runs."""
    env.data_dir.mkdir(parents=True, exist_ok=True)
    db = await Database.open(env.db_path)
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "upload_id": None,
        "file_url": "https://example.com/a.mp3",
        "provider": "cloud",
        "options": JobOptions(),
        "stage": "queued",
        "progress": 0.0,
        "created_at": now,
    }
    await db.insert_job(JobOut(id="was-running", status="running", **base))
    await db.insert_job(JobOut(id="was-queued", status="queued", **base))
    await db.close()

    provider = FakeProvider()
    # The lifespan builds its own manager; replace the module-level default factory.
    monkeypatch.setattr(jobs_module, "default_provider_factory", FakeFactory(provider))
    app = create_app(env)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        running = (await c.get("/api/jobs/was-running")).json()
        assert running["status"] == "failed" and running["error"] == INTERRUPTED
        assert running["finished_at"] is not None
        done = await wait_status(c, "was-queued", {"succeeded", "failed"})
        assert done["status"] == "succeeded"
        assert provider.calls[0][0].url == "https://example.com/a.mp3"


async def test_stop_is_clean_with_running_job(env: EnvSettings) -> None:
    env.data_dir.mkdir(parents=True, exist_ok=True)
    db = await Database.open(env.db_path)
    provider = FakeProvider(block=True)
    manager = JobManager(db=db, env=env, provider_factory=FakeFactory(provider))
    await manager.start()
    job = JobOut(
        id="j1",
        upload_id=None,
        file_url="https://example.com/a.mp3",
        provider="cloud",
        options=JobOptions(),
        status="queued",
        stage="queued",
        progress=0.0,
        created_at=datetime.now(UTC),
    )
    await db.insert_job(job)
    manager.enqueue(job)
    await wait_for(provider.started.is_set)
    await manager.stop()
    # The provider thread aborts at its next progress call.
    await asyncio.to_thread(time.sleep, 0.1)
    stored = await db.get_job("j1")
    assert stored is not None and stored.status == "running"  # marked interrupted on next start
    await db.close()


@pytest.fixture
async def started_manager(env: EnvSettings) -> AsyncIterator[tuple[JobManager, Database]]:
    env.data_dir.mkdir(parents=True, exist_ok=True)
    db = await Database.open(env.db_path)
    manager = JobManager(db=db, env=env)
    await manager.start()
    try:
        yield manager, db
    finally:
        await manager.stop()
        await db.close()


async def test_default_factory_is_used_and_missing_key_fails(
    started_manager: tuple[JobManager, Database],
    env_factory: EnvFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, db = started_manager
    seen: dict[str, Any] = {}

    def fake_get_provider(name: str, **config: Any) -> FakeProvider:
        seen.update(name=name, **config)
        return FakeProvider()

    monkeypatch.setattr("asr.providers.get_provider", fake_get_provider)
    job = JobOut(
        id="j2",
        upload_id=None,
        file_url="https://example.com/a.mp3",
        provider="cloud",
        options=JobOptions(cloud={"model": "custom-model"}),
        status="queued",
        stage="queued",
        progress=0.0,
        created_at=datetime.now(UTC),
    )
    await db.insert_job(job)
    manager.enqueue(job)
    deadline = time.monotonic() + 10
    while (stored := await db.get_job("j2")) and stored.status not in ("succeeded", "failed"):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    assert stored is not None and stored.status == "succeeded"
    assert seen["name"] == "cloud"
    assert seen["model"] == "custom-model"
    assert seen["api_key"] == DASHSCOPE_KEY
    assert seen["endpoint"] == "https://dashscope.aliyuncs.com"
    assert seen["file_transfer"] == "temp_upload"
    assert seen["work_dir"] == manager.env.data_dir / "work" / "j2"

    # Without any key the job fails with a clear message (no provider call).
    await db.set_setting("cloud.api_key", "")
    manager.env = env_factory()  # same dirs, no DASHSCOPE_API_KEY
    seen.clear()
    job3 = job.model_copy(update={"id": "j3"})
    await db.insert_job(job3)
    manager.enqueue(job3)
    deadline = time.monotonic() + 10
    while (stored := await db.get_job("j3")) and stored.status not in ("succeeded", "failed"):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    assert stored is not None and stored.status == "failed"
    assert stored.error == jobs_module.MISSING_KEY
    assert not seen
