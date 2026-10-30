"""Regression tests for the 2026-10-30 server review (open mode on the network, job finish races,
cross-device moves, lost transcript updates, body size cap, migrations, deploy hardening)."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from typer.testing import CliRunner

from asr.providers.base import Correction
from asr.summarize import TranscriptSummary
from server import db as db_module
from server import jobs as jobs_module
from server import storage
from server.app import create_app
from server.body_limit import MAX_JSON_BODY
from server.config import EnvSettings
from server.db import Database
from server.routes import transcripts as transcripts_route
from tests.conftest import EnvFactory
from tests.test_audio_fixtures import wav_bytes
from tests.test_jobs import DASHSCOPE_KEY, FakeProvider, add_upload, install, wait_for, wait_status

REPO = Path(__file__).resolve().parents[2]
LAN_PEER = ("192.168.1.50", 50123)


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


@asynccontextmanager
async def _peer_client(
    env: EnvSettings, peer: tuple[str, int], headers: dict[str, str] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(env)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=peer),
            base_url="http://localhost",
            headers=headers,
        ) as c,
    ):
        yield c


# ------------------------------------------------------- 1. open mode is loopback-peer only
async def test_open_mode_rejects_spoofed_host_from_lan_peer(env: EnvSettings) -> None:
    async with _peer_client(env, LAN_PEER, {"Host": "localhost"}) as c:
        r = await c.get("/api/settings")
        assert r.status_code == 403
        assert "ASR_API_KEY" in r.json()["detail"]
        assert (await c.get("/api/jobs")).status_code == 403
    for peer in (("127.0.0.1", 1), ("::1", 1), ("::ffff:127.0.0.1", 1)):
        async with _peer_client(env, peer, {"Host": "localhost"}) as c:
            assert (await c.get("/api/settings")).status_code == 200, peer


async def test_open_mode_network_opt_in(env_factory: EnvFactory) -> None:
    env = env_factory(ASR_ALLOW_UNAUTHENTICATED_NETWORK="1")
    assert env.allow_unauthenticated_network
    async with _peer_client(env, LAN_PEER, {"Host": "localhost"}) as c:
        assert (await c.get("/api/settings")).status_code == 200
    # The Host check (DNS rebinding) still applies with the opt-in.
    async with _peer_client(env, LAN_PEER, {"Host": "rebind.evil.example"}) as c:
        assert (await c.get("/api/settings")).status_code == 403


async def test_lan_peer_with_api_key_is_fine(env_factory: EnvFactory) -> None:
    env = env_factory(ASR_API_KEY="k" * 20)
    async with _peer_client(env, LAN_PEER, {"X-API-Key": "k" * 20, "Host": "asr.lan"}) as c:
        assert (await c.get("/api/settings")).status_code == 200


def _serve(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> tuple[Any, list[dict[str, Any]]]:
    import uvicorn

    from server import cli as server_cli

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return CliRunner().invoke(server_cli.app, ["serve", *args]), calls


def test_serve_refuses_network_bind_without_key(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_factory()
    for host in ("0.0.0.0", "::", "192.168.1.10"):  # noqa: S104
        result, calls = _serve(monkeypatch, ["--host", host])
        assert result.exit_code == 2, result.output
        assert "ASR_ALLOW_UNAUTHENTICATED_NETWORK" in result.output
        assert not calls
    result, calls = _serve(monkeypatch, ["--host", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "127.0.0.1"


def test_serve_network_bind_with_key_or_opt_in(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_factory(ASR_API_KEY="k" * 20)
    result, calls = _serve(monkeypatch, ["--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 0, result.output
    assert calls and "warning" not in result.output

    env_factory(ASR_ALLOW_UNAUTHENTICATED_NETWORK="true")
    result, calls = _serve(monkeypatch, ["--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 0, result.output
    assert calls and "warning" in result.output


def test_compose_opts_in_only_with_loopback_publish() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ASR_ALLOW_UNAUTHENTICATED_NETWORK" in compose
    assert '"${ASR_BIND_ADDR:-127.0.0.1}:${ASR_BACKEND_PORT:-8000}:8000"' in compose
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    # The image itself does not opt in: without a key and without compose it refuses to start.
    assert "ASR_ALLOW_UNAUTHENTICATED_NETWORK" not in re.sub(r"#.*", "", dockerfile)


# -------------------------------------------- 2. cancel / delete during slow post-correction
@pytest.fixture
def slow_post_correct(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    real = jobs_module.correct_segments

    def slow(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(jobs_module, "correct_segments", slow)
    return entered, release


async def test_cancel_during_post_correction_cancels(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    slow_post_correct: tuple[threading.Event, threading.Event],
) -> None:
    entered, release = slow_post_correct
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_for(entered.is_set)
    assert app.state.job_manager.is_running(job["id"])
    r = await client.post(f"/api/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    release.set()
    done = await wait_status(client, job["id"], {"cancelled", "succeeded", "failed"})
    assert done["status"] == "cancelled"
    assert done["transcript_id"] is None


async def test_delete_during_post_correction_is_quiet(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    slow_post_correct: tuple[threading.Event, threading.Event],
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered, release = slow_post_correct
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_for(entered.is_set)
    with caplog.at_level(logging.ERROR, logger="server.jobs"):
        assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 204
        release.set()
        await wait_for(lambda: not app.state.job_manager.is_running(job["id"]))
        await asyncio.sleep(0.1)
    assert not [r for r in caplog.records if "crashed" in r.getMessage()]
    assert (await client.get(f"/api/jobs/{job['id']}")).status_code == 404


async def test_insert_after_delete_race_is_quiet(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The row vanishes between the final status check and the transcript insert."""
    db: Database = app.state.db
    real_insert = db.insert_transcript

    async def delete_then_insert(transcript_id: str, job_id: str, *a: Any, **kw: Any) -> Any:
        await db.delete_job(job_id)
        return await real_insert(transcript_id, job_id, *a, **kw)

    monkeypatch.setattr(db, "insert_transcript", delete_then_insert)
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    with caplog.at_level(logging.ERROR, logger="server.jobs"):
        job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
        await wait_for(lambda: job["id"] not in app.state.job_manager._running)
        await asyncio.sleep(0.1)
    assert not [r for r in caplog.records if "crashed" in r.getMessage()]


# ----------------------------------------------------------- 3. cross-device move_into
def _exdev(monkeypatch: pytest.MonkeyPatch, before: Callable[[Path], None] | None = None) -> None:
    def replace(src: Any, dst: Any) -> None:
        if before is not None:
            before(Path(dst))
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(storage.os, "replace", replace)


def test_move_into_cross_device_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src.part"
    src.write_bytes(b"audio" * 1000)
    target = tmp_path / "root"
    target.mkdir()
    _exdev(monkeypatch)
    dst = storage.move_into(src, target, "a.wav")
    assert dst.read_bytes() == b"audio" * 1000
    assert not src.exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")
@pytest.mark.parametrize("swap", ["symlink", "hardlink"])
def test_move_into_cross_device_refuses_swapped_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    src = tmp_path / "src.part"
    src.write_bytes(b"attacker controlled")
    target = tmp_path / "root"
    target.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")

    def swap_dst(dst: Path) -> None:
        dst.unlink()
        if swap == "symlink":
            dst.symlink_to(victim)
        else:
            os.link(victim, dst)

    _exdev(monkeypatch, swap_dst)
    with pytest.raises(storage.PathNotAllowedError):
        storage.move_into(src, target, "a.wav")
    assert victim.read_bytes() == b"precious"


# ------------------------------------------------------------ 4. lost transcript updates
async def _transcript(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> dict[str, Any]:
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded"})
    return (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()


def _edit(segment_id: str, text: str) -> dict[str, str]:
    return {"op": "edit_text", "segment_id": segment_id, "text": text}


async def test_patch_with_stale_version_is_409(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    r = await client.patch(
        url, json={"ops": [_edit("s0", "A")], "expected_updated_at": t["updated_at"]}
    )
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["updated_at"] != t["updated_at"]

    r = await client.patch(
        url, json={"ops": [_edit("s1", "B")], "expected_updated_at": t["updated_at"]}
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "transcript changed since it was loaded"}
    after = (await client.get(url)).json()
    assert after["segments"][1]["text"] == t["segments"][1]["text"]  # nothing written

    # A millisecond-precision copy (JS Date round trip) of the current version still matches.
    ms = datetime.fromisoformat(first["updated_at"]).replace(microsecond=0) + timedelta(
        milliseconds=datetime.fromisoformat(first["updated_at"]).microsecond // 1000
    )
    body = {"ops": [_edit("s1", "B")], "expected_updated_at": ms.isoformat()}
    assert (await client.patch(url, json=body)).status_code == 200

    r = await client.put(
        f"{url}/speakers",
        json={
            "speakers": [{"id": "SPEAKER_00", "name": "X"}],
            "expected_updated_at": t["updated_at"],
        },
    )
    assert r.status_code == 409
    # Without the field the old behaviour stays (desktop / CLI clients).
    r = await client.put(f"{url}/speakers", json={"speakers": [{"id": "SPEAKER_00", "name": "X"}]})
    assert r.status_code == 200


@pytest.fixture
def interleave_reads(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every transcript read yields, so two requests read the same version before writing."""
    db: Database = app.state.db
    real = db.get_transcript

    async def slow_get(transcript_id: str) -> Any:
        row = await real(transcript_id)
        await asyncio.sleep(0.05)
        return row

    monkeypatch.setattr(db, "get_transcript", slow_get)


async def test_concurrent_patches_never_lose_an_edit(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, interleave_reads: None
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    a, b = await asyncio.gather(
        client.patch(url, json={"ops": [_edit("s0", "first")]}),
        client.patch(url, json={"ops": [_edit("s1", "second")]}),
    )
    assert a.status_code == b.status_code == 200
    final = (await client.get(url)).json()
    assert [s["text"] for s in final["segments"][:2]] == ["first", "second"]


async def test_concurrent_patches_same_version_one_wins(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, interleave_reads: None
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    v = t["updated_at"]
    a, b = await asyncio.gather(
        client.patch(url, json={"ops": [_edit("s0", "first")], "expected_updated_at": v}),
        client.patch(url, json={"ops": [_edit("s1", "second")], "expected_updated_at": v}),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]


class _BlockingLlm:
    """Stands in for ``LlmCorrector``; ``block`` makes ``suggest`` wait for ``release``."""

    block = False
    entered = threading.Event()
    release = threading.Event()
    result: ClassVar[list[Correction]] = []

    def __init__(self, **_: Any) -> None:
        pass

    def suggest(self, segments: Any, terms: Any, context: Any) -> tuple[list[Correction], int]:
        if self.block:
            type(self).entered.set()
            assert type(self).release.wait(10)
        return [c.model_copy() for c in self.result], 0


def _suggestion(segment: dict[str, Any], before: str, after: str) -> Correction:
    return Correction(
        segment_id=segment["id"],
        source="llm",
        before=before,
        after=after,
        status="suggested",
        offset=segment["text"].index(before),
    )


async def test_llm_suggestions_merge_into_current_row(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    s0, s1, s2 = t["segments"]

    class Llm(_BlockingLlm):
        entered = threading.Event()
        release = threading.Event()

    monkeypatch.setattr(transcripts_route, "LlmCorrector", Llm)
    Llm.result = [_suggestion(s1, "chip", "Chip")]
    first = (await client.post(f"{url}/llm-suggestions", json={})).json()
    chip = next(c for c in first["transcript"]["corrections"] if c["source"] == "llm")

    Llm.block = True
    Llm.result = [_suggestion(s0, "测试", "测验"), _suggestion(s2, "driver", "Driver")]
    task = asyncio.create_task(client.post(f"{url}/llm-suggestions", json={}))
    await wait_for(Llm.entered.is_set)
    # Meanwhile: the earlier suggestion is accepted and segment s2 is edited.
    review = {"op": "review_correction", "correction_id": chip["id"], "decision": "accept"}
    assert (await client.patch(url, json={"ops": [review]})).status_code == 200
    assert (
        await client.patch(url, json={"ops": [_edit("s2", "然后是 driver 部分。")]})
    ).status_code == 200
    Llm.release.set()
    r = await task
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["added"] == 1  # s2 changed meanwhile: its suggestion is dropped
    by_after = {c["after"]: c for c in out["transcript"]["corrections"] if c["source"] == "llm"}
    assert by_after["Chip"]["status"] == "accepted"  # review kept
    assert by_after["测验"]["status"] == "suggested"
    assert "Driver" not in by_after
    texts = [s["text"] for s in out["transcript"]["segments"]]
    assert "Chip" in texts[1] and texts[2] == "然后是 driver 部分。"


async def test_summary_of_edited_text_is_marked_stale(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    entered, release = threading.Event(), threading.Event()

    class Summ:
        def __init__(self, **_: Any) -> None:
            pass

        def summarize(self, *_: Any) -> TranscriptSummary:
            entered.set()
            assert release.wait(10)
            return TranscriptSummary(
                overview="o", model="m", created_at=datetime.now().astimezone()
            )

    monkeypatch.setattr(transcripts_route, "Summarizer", Summ)
    task = asyncio.create_task(client.post(f"{url}/summary"))
    await wait_for(entered.is_set)
    edited = (await client.patch(url, json={"ops": [_edit("s0", "改了")]})).json()
    release.set()
    r = await task
    assert r.status_code == 200, r.text
    created = datetime.fromisoformat(r.json()["created_at"])
    assert created < datetime.fromisoformat(edited["updated_at"])  # stale (D50)
    assert created == datetime.fromisoformat(t["updated_at"])


@pytest.mark.parametrize("name", ["a\nb", "a\rb", "tab\there", "nul\x00", " x", "x‮"])
async def test_speaker_name_rejects_control_characters(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, name: str
) -> None:
    t = await _transcript(app, client, env)
    r = await client.put(
        f"/api/transcripts/{t['id']}/speakers",
        json={"speakers": [{"id": "SPEAKER_00", "name": name}]},
    )
    assert r.status_code == 422
    assert name not in r.text


# ----------------------------------------------------------------- 5. JSON body size cap
async def test_json_body_over_2mb_is_413(client: httpx.AsyncClient) -> None:
    big = b'{"name": "x", "terms": [], "pad": "' + b"x" * MAX_JSON_BODY + b'"}'
    headers = {"Content-Type": "application/json"}
    r = await client.post("/api/term-sets", content=big, headers=headers)
    assert r.status_code == 413

    async def stream() -> AsyncIterator[bytes]:  # no Content-Length
        for i in range(0, len(big), 256 * 1024):
            yield big[i : i + 256 * 1024]

    r = await client.post("/api/term-sets", content=stream(), headers=headers)
    assert r.status_code == 413
    # Small bodies still work.
    r = await client.post("/api/term-sets", json={"name": "ok", "terms": []})
    assert r.status_code == 201, r.text


async def test_upload_routes_keep_their_own_limits(client: httpx.AsyncClient) -> None:
    pad = b"\0" * (MAX_JSON_BODY + 1024)
    r = await client.post(
        "/api/uploads",
        data={"target_dir": ""},
        files={"file": ("big.wav", wav_bytes(0.2) + pad)},
    )
    assert r.status_code != 413


# -------------------------------------------------------------------- 6. atomic migrations
_V1_UPLOADS = """CREATE TABLE uploads (id TEXT PRIMARY KEY, path TEXT NOT NULL,
    original_name TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL,
    probe_json TEXT)"""
_V1_TRANSCRIPTS = """CREATE TABLE transcripts (id TEXT PRIMARY KEY, job_id TEXT NOT NULL
    REFERENCES jobs(id) ON DELETE CASCADE, provider TEXT NOT NULL, duration REAL, language TEXT,
    speakers_json TEXT NOT NULL, segments_json TEXT NOT NULL, original_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


def _v1_database(path: Path, *, extra: tuple[str, ...] = ()) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00+00:00')")
    conn.execute(_V1_UPLOADS)
    conn.execute(_V1_TRANSCRIPTS)
    for stmt in extra:
        conn.execute(stmt)
    conn.commit()
    conn.close()


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


async def test_half_applied_migration_still_opens(tmp_path: Path) -> None:
    path = tmp_path / "asr.db"
    # An older release crashed after adding v2's column but before recording the version.
    _v1_database(
        path, extra=("ALTER TABLE uploads ADD COLUMN registered INTEGER NOT NULL DEFAULT 0",)
    )
    db = await Database.open(path)
    try:
        assert await db.schema_version() == db_module.SCHEMA_VERSION
    finally:
        await db.close()
    assert {"corrections_json", "summary_json"} <= _columns(path, "transcripts")


async def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "asr.db"
    _v1_database(path)
    broken = (*db_module.MIGRATIONS[:2], (4, "transcripts", "summary_json", "TEXT BOGUS("))
    monkeypatch.setattr(db_module, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.Error):
        await Database.open(path)
    # Nothing of the failed run stuck: no column, no version.
    assert "registered" not in _columns(path, "uploads")
    assert "corrections_json" not in _columns(path, "transcripts")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    conn.close()

    monkeypatch.undo()
    db = await Database.open(path)
    try:
        assert await db.schema_version() == db_module.SCHEMA_VERSION
    finally:
        await db.close()


# ------------------------------------------------------------------------ 7./8. deploy files
def test_dockerfile_keeps_app_code_read_only() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        if "chown" in line:
            assert "/app" not in line and "/opt" not in line, line
    assert dockerfile.count("UV_COMPILE_BYTECODE=1") == 2  # no .pyc writes at runtime


def test_nginx_security_headers_in_every_header_block() -> None:
    conf = (REPO / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    required = (
        'X-Frame-Options "DENY"',
        "Content-Security-Policy \"frame-ancestors 'none'\"",
        'X-Content-Type-Options "nosniff"',
    )
    locations = re.findall(r"^\s*location[^{\n]*\{[^{}]*\}", conf, flags=re.M)
    server_level = conf
    for block in locations:
        server_level = server_level.replace(block, "")
    # nginx drops server-level add_header in any location that has its own add_header.
    for block in [server_level, *(b for b in locations if "add_header" in b)]:
        for header in required:
            assert header in block, (header, block[:40])


# --------------------------------------------------------- chunked complete is idempotent
async def test_chunked_complete_is_idempotent(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 16 * 1024)
    data = wav_bytes(1.0)
    init = (
        await client.post(
            "/api/uploads/init", json={"filename": "idem.wav", "size": len(data), "target_dir": ""}
        )
    ).json()
    sid, size = init["session_id"], init["part_size"]
    for n in range(init["total_parts"]):
        part = data[n * size : (n + 1) * size]
        assert (await client.put(f"/api/uploads/{sid}/parts/{n}", content=part)).status_code == 200
    first, second = await asyncio.gather(
        client.post(f"/api/uploads/{sid}/complete"), client.post(f"/api/uploads/{sid}/complete")
    )
    assert sorted([first.status_code, second.status_code]) == [200, 201], (first.text, second.text)
    assert first.json() == second.json()
    third = await client.post(f"/api/uploads/{sid}/complete")
    assert third.status_code == 200 and third.json() == first.json()
    assert (await client.post(f"/api/uploads/{'x' * 32}/complete")).status_code == 404


# -------------------------------------------------- browser audio plays the job's stream
async def test_audio_uses_job_stream_index(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.routes import audio as audio_route
    from tests.test_audio_fixtures import write_multistream

    install(app, FakeProvider())  # the job must never reach the real cloud provider
    calls: list[int] = []

    def fake_transcode(src: Path, dst: Path, *, stream_index: int = 0) -> Path:
        calls.append(stream_index)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"mp3")
        return dst

    monkeypatch.setattr(audio_route, "transcode_for_browser", fake_transcode)
    src = write_multistream(tmp_path)
    up = (
        await client.post(
            "/api/uploads", files={"file": ("obs.mkv", src.read_bytes())}, data={"target_dir": ""}
        )
    ).json()
    assert (await client.get(f"/api/audio/{up['id']}")).status_code == 200
    assert calls == [0]  # no job yet

    options = {"provider": "cloud", "preprocess": {"stream_index": 1}}
    r = await client.post("/api/jobs", json={"upload_id": up["id"], "options": options})
    assert r.status_code == 201, r.text
    assert (await client.get(f"/api/audio/{up['id']}")).status_code == 200
    assert calls == [0, 1]
    assert (env.data_dir / "cache" / "audio" / f"{up['id']}.s1.mp3").is_file()
    # Explicit choice wins; cached afterwards; out-of-range streams are a 422.
    assert (await client.get(f"/api/audio/{up['id']}?stream_index=0")).status_code == 200
    assert calls == [0, 1]
    assert (await client.get(f"/api/audio/{up['id']}?stream_index=2")).status_code == 422
