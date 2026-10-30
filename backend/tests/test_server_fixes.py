"""Regression tests for the server review at db52182 (docs/review/server.md): M1 disk budget and
full-disk handling, m1 stale complete lock, m2 SSE subscriber leak, m3 open mode behind
forwarders, m6 delete_audio on Windows, m7 body cap on JSON upload routes, m8 Windows path
shapes, n1 LLM error mapping, n2 shared containment check, n7 non-ASCII API key."""

# Blocking filesystem checks inside async tests are fine (tmp dirs, no event-loop load).
# ruff: noqa: ASYNC240

from __future__ import annotations

import asyncio
import errno
import ntpath
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.types import Message, Receive, Scope, Send
from typer.testing import CliRunner

from asr.providers.base import ProviderError
from server import storage
from server.auth import ApiKeyMiddleware
from server.body_limit import MAX_JSON_BODY
from server.config import EnvSettings
from server.routes import jobs as jobs_route
from server.routes import transcripts as transcripts_route
from server.routes import uploads as uploads_route
from tests.conftest import ClientFactory, EnvFactory
from tests.fs_helpers import junction_or_skip
from tests.test_audio_fixtures import wav_bytes
from tests.test_jobs import DASHSCOPE_KEY, FakeProvider, add_upload, install, wait_for, wait_status

BACKEND = Path(__file__).resolve().parents[1]
MARGIN = storage.FREE_SPACE_MARGIN


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


@pytest.fixture
def roots(tmp_path: Path) -> list[Path]:
    root = tmp_path / "uploads"
    root.mkdir()
    return [root.resolve()]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d.resolve()


def _free(monkeypatch: pytest.MonkeyPatch, free: Callable[[Path], int]) -> None:
    """``shutil.disk_usage`` reports ``free(path)`` free bytes."""
    real = shutil.disk_usage

    def fake(path: str | os.PathLike[str]) -> Any:
        usage = real(path)
        left = free(Path(path))
        return type(usage)(usage.total, max(0, usage.total - left), left)

    monkeypatch.setattr(storage.shutil, "disk_usage", fake)


def _two_volumes(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Pretend ``data_dir`` is on device 1 and everything else on device 2."""
    real = storage._device

    def device(path: Path) -> int | None:
        if real(path) is None:
            return None
        return 1 if Path(path).resolve().is_relative_to(data_dir) else 2

    monkeypatch.setattr(storage, "_device", device)


def _upload_parts(data_dir: Path, session: storage.UploadSession, data: bytes) -> None:
    for n in range(session.total_parts):
        chunk = data[n * session.part_size : (n + 1) * session.part_size]
        tmp, f = storage.open_part_writer(data_dir, session, n)
        f.write(chunk)
        f.close()
        storage.commit_part(data_dir, session, n, tmp)


def _session_dir(data_dir: Path, session: storage.UploadSession) -> Path:
    return storage.sessions_dir(data_dir) / session.session_id


# ------------------------------------------------------------------- M1: disk budget at init
def test_init_needs_twice_the_size_on_one_volume(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    size = 10_000
    # Parts and assembled.tmp coexist under the data dir; the move is a rename.
    _free(monkeypatch, lambda _: 2 * size + MARGIN - 1)
    with pytest.raises(storage.InsufficientStorageError):
        storage.create_session(data_dir, roots, filename="a.wav", size=size, target_dir="")
    _free(monkeypatch, lambda _: 2 * size + MARGIN)
    storage.create_session(data_dir, roots, filename="a.wav", size=size, target_dir="")


def test_init_cross_volume_needs_a_copy_on_the_target(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    size = 10_000
    _two_volumes(monkeypatch, data_dir)
    target_free = {"n": size + MARGIN - 1}

    def free(path: Path) -> int:
        return 2 * size + MARGIN if path.resolve().is_relative_to(data_dir) else target_free["n"]

    _free(monkeypatch, free)
    with pytest.raises(storage.InsufficientStorageError):
        storage.create_session(data_dir, roots, filename="a.wav", size=size, target_dir="")
    target_free["n"] = size + MARGIN
    storage.create_session(data_dir, roots, filename="a.wav", size=size, target_dir="")


def test_init_reserves_space_promised_to_other_sessions(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    size = 10_000
    _free(monkeypatch, lambda _: 3 * size + MARGIN)
    storage.create_session(data_dir, roots, filename="a.wav", size=size, target_dir="")
    # The first session still needs 2 x size; a second one would need another 2 x size.
    with pytest.raises(storage.InsufficientStorageError):
        storage.create_session(data_dir, roots, filename="b.wav", size=size, target_dir="")
    # Abandoned sessions stop reserving once they are idle.
    old = storage.time.time() - storage.ACTIVE_SESSION_S - 60
    for d in storage.sessions_dir(data_dir).iterdir():
        os.utime(d, (old, old))
    storage.create_session(data_dir, roots, filename="b.wav", size=size, target_dir="")


def test_reservation_shrinks_as_parts_arrive(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1000)
    size = 4000
    first = storage.create_session(data_dir, roots, filename="a.wav", size=size, target_dir="")
    _upload_parts(data_dir, first, os.urandom(size))
    # Written parts already left the free count: only the assembled copy is still owed.
    assert storage._reserved_by_sessions(data_dir) == [(data_dir, size)]


# ---------------------------------------------------------------- M1: disk budget at complete
def test_complete_rechecks_space_and_keeps_the_parts(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1000)
    data = os.urandom(2500)
    s = storage.create_session(data_dir, roots, filename="a.wav", size=len(data), target_dir="")
    _upload_parts(data_dir, s, data)
    _free(monkeypatch, lambda _: len(data) + MARGIN - 1)
    with pytest.raises(storage.InsufficientStorageError):
        storage.complete_session(data_dir, roots, s.session_id)
    assert storage.missing_parts(data_dir, s) == []
    assert not (_session_dir(data_dir, s) / storage.ASSEMBLED).exists()
    _free(monkeypatch, lambda _: len(data) + MARGIN)
    stored, _ = storage.complete_session(data_dir, roots, s.session_id)
    assert stored.read_bytes() == data


def test_complete_cross_volume_checks_the_target(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1000)
    data = os.urandom(2500)
    s = storage.create_session(data_dir, roots, filename="a.wav", size=len(data), target_dir="")
    _upload_parts(data_dir, s, data)
    _two_volumes(monkeypatch, data_dir)

    def free(path: Path) -> int:
        inside = path.resolve().is_relative_to(data_dir)
        return len(data) + MARGIN if inside else len(data) + MARGIN - 1

    _free(monkeypatch, free)
    with pytest.raises(storage.InsufficientStorageError):
        storage.complete_session(data_dir, roots, s.session_id)
    assert storage.missing_parts(data_dir, s) == []


def test_enospc_while_copying_across_volumes_cleans_up(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1000)
    data = os.urandom(2500)
    s = storage.create_session(data_dir, roots, filename="a.wav", size=len(data), target_dir="")
    _upload_parts(data_dir, s, data)

    with monkeypatch.context() as m:

        def exdev(src: Any, dst: Any) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        def full_copy(fin: Any, fout: Any, length: int = 0) -> None:
            fout.write(fin.read(100))
            raise OSError(errno.ENOSPC, "No space left on device")

        m.setattr(storage.os, "replace", exdev)
        m.setattr(storage.shutil, "copyfileobj", full_copy)
        with pytest.raises(storage.InsufficientStorageError, match="disk space"):
            storage.complete_session(data_dir, roots, s.session_id)
    assert os.listdir(roots[0]) == []  # the partial copy is gone
    assert not (_session_dir(data_dir, s) / storage.ASSEMBLED).exists()
    assert storage.missing_parts(data_dir, s) == []  # retry possible
    stored, _ = storage.complete_session(data_dir, roots, s.session_id)
    assert stored.read_bytes() == data


def test_disk_full_errors_are_recognised() -> None:
    assert storage.is_disk_full(OSError(errno.ENOSPC, "full"))
    if hasattr(errno, "EDQUOT"):
        assert storage.is_disk_full(OSError(errno.EDQUOT, "quota"))
    win = OSError(errno.EINVAL, "disk full")
    win.winerror = 112  # type: ignore[attr-defined]
    assert storage.is_disk_full(win)
    assert not storage.is_disk_full(OSError(errno.EACCES, "denied"))
    assert not storage.is_disk_full(ValueError("x"))


# ------------------------------------------------------------- M1: routes answer 507, not 500
class _FullFile:
    """A file whose writes fail with ENOSPC."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def write(self, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self) -> None:
        self.inner.close()


async def test_part_write_enospc_is_507_and_removes_the_temp_part(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1024)
    init = await client.post(
        "/api/uploads/init", json={"filename": "a.wav", "size": 2048, "target_dir": ""}
    )
    sid = init.json()["session_id"]
    real = storage.open_part_writer

    def full(*args: Any) -> tuple[Path, Any]:
        path, fh = real(*args)
        return path, _FullFile(fh)

    monkeypatch.setattr(storage, "open_part_writer", full)
    r = await client.put(f"/api/uploads/{sid}/parts/0", content=b"x" * 1024)
    assert r.status_code == 507, r.text
    assert "disk space" in r.json()["detail"]
    left = os.listdir(storage.sessions_dir(env.data_dir) / sid)
    assert left == [storage.MANIFEST]


async def test_multipart_checks_space_up_front(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, lambda _: 1024)
    r = await client.post(
        "/api/uploads", files={"file": ("a.wav", wav_bytes(0.2))}, data={"target_dir": ""}
    )
    assert r.status_code == 507, r.text
    tmp = env.data_dir / storage.TMP_DIRNAME
    assert not tmp.exists() or os.listdir(tmp) == []


async def test_multipart_write_enospc_is_507_and_removes_the_temp_file(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = storage.new_temp_file

    def full(data_dir: Path) -> tuple[Path, Any]:
        path, fh = real(data_dir)
        return path, _FullFile(fh)

    monkeypatch.setattr(storage, "new_temp_file", full)
    r = await client.post(
        "/api/uploads", files={"file": ("a.wav", wav_bytes(0.2))}, data={"target_dir": ""}
    )
    assert r.status_code == 507, r.text
    assert os.listdir(env.data_dir / storage.TMP_DIRNAME) == []


async def test_complete_enospc_is_507_then_retry_succeeds(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 16 * 1024)
    data = wav_bytes(1.0)
    init = await client.post(
        "/api/uploads/init", json={"filename": "full.wav", "size": len(data), "target_dir": ""}
    )
    body = init.json()
    for n in range(body["total_parts"]):
        part = data[n * body["part_size"] : (n + 1) * body["part_size"]]
        r = await client.put(f"/api/uploads/{body['session_id']}/parts/{n}", content=part)
        assert r.status_code == 200
    with monkeypatch.context() as m:

        def full(*args: Any) -> Path:
            raise OSError(errno.ENOSPC, "No space left on device")

        m.setattr(storage, "move_into", full)
        r = await client.post(f"/api/uploads/{body['session_id']}/complete")
        assert r.status_code == 507, r.text
    sdir = storage.sessions_dir(env.data_dir) / body["session_id"]
    assert not (sdir / storage.ASSEMBLED).exists()
    r = await client.post(f"/api/uploads/{body['session_id']}/complete")
    assert r.status_code == 201, r.text
    assert Path(r.json()["path"]).read_bytes() == data


# ------------------------------------------------------------------- m1: stale complete lock
def _ready_session(data_dir: Path, roots: list[Path]) -> tuple[storage.UploadSession, bytes]:
    data = os.urandom(3000)
    s = storage.create_session(data_dir, roots, filename="a.wav", size=len(data), target_dir="")
    _upload_parts(data_dir, s, data)
    return s, data


def test_lock_file_left_by_a_crash_does_not_block(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1000)
    s, data = _ready_session(data_dir, roots)
    sdir = _session_dir(data_dir, s)
    (sdir / storage.COMPLETE_LOCK).write_bytes(b"")  # what a killed process leaves behind
    (sdir / storage.ASSEMBLED).write_bytes(b"half")  # ... with a partial assembly
    stored, _ = storage.complete_session(data_dir, roots, s.session_id)
    assert stored.read_bytes() == data


def test_live_lock_holder_conflicts_until_it_dies(
    data_dir: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 1000)
    s, data = _ready_session(data_dir, roots)
    lock = _session_dir(data_dir, s) / storage.COMPLETE_LOCK
    code = (
        "import os, sys, time\n"
        "from server import storage\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0))\n"
        "assert storage.try_lock(fd)\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)], cwd=BACKEND, stdout=subprocess.PIPE
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == b"locked"
        with pytest.raises(storage.ConflictError):
            storage.complete_session(data_dir, roots, s.session_id)
    finally:
        holder.kill()  # a crash: no cleanup code runs in the holder
        holder.wait(10)
    stored, _ = storage.complete_session(data_dir, roots, s.session_id)
    assert stored.read_bytes() == data


def test_lock_is_exclusive_within_one_process(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    a = os.open(path, os.O_RDWR | os.O_CREAT)
    b = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert storage.try_lock(a)
        assert not storage.try_lock(b)
    finally:
        os.close(a)
    try:
        assert storage.try_lock(b)  # released with the holder's file
    finally:
        os.close(b)


# ----------------------------------------------------------------------- m2: SSE subscribers
async def test_sse_subscribes_only_once_the_stream_runs(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    blocker = FakeProvider(block=True)
    install(app, blocker)
    manager = app.state.job_manager
    job = (await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})).json()
    await wait_for(blocker.started.is_set)
    # A response that is built but torn down before streaming starts leaves nothing behind.
    response = await jobs_route.job_events(job["id"], None, manager)  # type: ignore[arg-type]
    assert manager.subscriber_count(job["id"]) == 0
    del response
    blocker.release.set()
    await wait_status(client, job["id"], {"succeeded"})


async def test_sse_client_disconnect_unsubscribes(app: FastAPI, client: httpx.AsyncClient) -> None:
    blocker = FakeProvider(block=True)
    install(app, blocker)
    manager = app.state.job_manager
    job = (await client.post("/api/jobs", json={"file_url": "https://example.com/a.mp3"})).json()
    await wait_for(blocker.started.is_set)

    first_event = asyncio.Event()
    requested = False

    async def receive() -> Message:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_event.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and b"event: job" in message.get("body", b""):
            assert manager.subscriber_count(job["id"]) == 1
            first_event.set()

    path = f"/api/jobs/{job['id']}/events"
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), 10)
    assert first_event.is_set()
    await wait_for(lambda: manager.subscriber_count(job["id"]) == 0)
    assert manager.is_running(job["id"])  # the job itself is unaffected
    blocker.release.set()
    await wait_status(client, job["id"], {"succeeded"})


# --------------------------------------------------------------- m3: open mode and forwarders
@pytest.mark.parametrize(
    "header",
    [
        ("X-Forwarded-For", "192.168.1.50"),
        ("Forwarded", "for=192.168.1.50"),
        ("X-Real-IP", "192.168.1.50"),
        ("Via", "1.1 nginx"),
    ],
)
async def test_open_mode_refuses_proxied_requests(
    client: httpx.AsyncClient, header: tuple[str, str]
) -> None:
    assert (await client.get("/api/jobs")).status_code == 200
    r = await client.get("/api/jobs", headers=dict([header]))
    assert r.status_code == 403
    assert "ASR_API_KEY" in r.json()["detail"]


async def test_proxied_requests_are_fine_with_a_key(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    env = env_factory(ASR_API_KEY="k" * 20)
    headers = {"X-API-Key": "k" * 20, "X-Forwarded-For": "192.168.1.50", "Via": "1.1 nginx"}
    async with make_client(env, headers=headers) as c:
        assert (await c.get("/api/jobs")).status_code == 200


async def _ok(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _status(app: Any, *, client: Any, headers: list[tuple[bytes, bytes]]) -> int:
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/jobs",
        "headers": [(b"host", b"localhost"), *headers],
        "client": client,
    }
    seen: dict[str, int] = {}

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]

    await app(scope, receive, send)
    return seen["status"]


async def test_open_mode_does_not_trust_unix_socket_peers() -> None:
    closed = ApiKeyMiddleware(_ok, api_key=SecretStr(""))
    assert await _status(closed, client=None, headers=[]) == 403
    assert await _status(closed, client=("127.0.0.1", 1), headers=[]) == 200
    opted_in = ApiKeyMiddleware(_ok, api_key=SecretStr(""), allow_network=True)
    assert await _status(opted_in, client=None, headers=[]) == 200


def _serve(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> tuple[Any, list[dict[str, Any]]]:
    import uvicorn

    from server import cli as server_cli

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return CliRunner().invoke(server_cli.app, ["serve", *args]), calls


def test_serve_refuses_a_proxy_without_a_key(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_factory()
    result, calls = _serve(monkeypatch, ["--forwarded-allow-ips", "127.0.0.1"])
    assert result.exit_code == 2, result.output
    assert "ASR_API_KEY" in result.output
    assert not calls
    # The network opt-in (Docker) does not cover a proxy either.
    env_factory(ASR_ALLOW_UNAUTHENTICATED_NETWORK="1")
    result, calls = _serve(
        monkeypatch,
        ["--host", "0.0.0.0", "--forwarded-allow-ips", "172.16.0.0/12"],  # noqa: S104
    )
    assert result.exit_code == 2, result.output
    assert not calls

    env_factory(ASR_API_KEY="k" * 20)
    result, calls = _serve(monkeypatch, ["--forwarded-allow-ips", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert calls[0]["proxy_headers"] is True
    assert calls[0]["forwarded_allow_ips"] == "127.0.0.1"


# ----------------------------------------------------------------- m6: delete_audio, Windows
async def test_delete_audio_keeps_the_record_when_the_file_is_in_use(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_status(client, job["id"], {"succeeded"})

    monkeypatch.setattr(jobs_route, "UNLINK_RETRY_S", 0.0)
    real_unlink = pathlib.Path.unlink
    stored = Path(upload.path)

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == stored:  # what Windows does while another handle has the file open
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    r = await client.delete(f"/api/jobs/{job['id']}?delete_audio=true")
    assert r.status_code == 204
    assert (await client.get(f"/api/jobs/{job['id']}")).status_code == 404
    assert stored.is_file()
    assert await app.state.db.get_upload(upload.id) is not None  # still points at the file


async def test_delete_audio_waits_for_a_running_job(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = FakeProvider(block=True)
    install(app, blocker)
    manager = app.state.job_manager
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_for(blocker.started.is_set)

    seen: list[bool] = []
    real = jobs_route._remove_audio_files

    def remove(*args: Any, **kwargs: Any) -> bool:
        seen.append(manager.is_running(job["id"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(jobs_route, "_remove_audio_files", remove)
    r = await client.delete(f"/api/jobs/{job['id']}?delete_audio=true")
    assert r.status_code == 204
    assert seen == [False]  # the run had let go of the file before it was removed
    assert not Path(upload.path).exists()
    assert await app.state.db.get_upload(upload.id) is None


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete open files")
async def test_delete_audio_with_the_file_held_open_windows(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    await wait_status(client, job["id"], {"succeeded"})
    monkeypatch.setattr(jobs_route, "UNLINK_RETRY_S", 0.0)
    with open(upload.path, "rb"):  # noqa: ASYNC230 - the point is to hold a handle
        r = await client.delete(f"/api/jobs/{job['id']}?delete_audio=true")
    assert r.status_code == 204
    assert Path(upload.path).is_file()
    assert await app.state.db.get_upload(upload.id) is not None


# ----------------------------------------------------------- m7: JSON upload routes are capped
async def test_json_upload_routes_are_capped(client: httpx.AsyncClient) -> None:
    pad = "x" * (MAX_JSON_BODY + 1)
    headers = {"Content-Type": "application/json"}
    for path, body in (
        ("/api/uploads/init", f'{{"filename": "a.wav", "size": 1, "target_dir": "{pad}"}}'),
        ("/api/uploads/register", f'{{"path": "{pad}"}}'),
    ):
        r = await client.post(path, content=body.encode(), headers=headers)
        assert r.status_code == 413, (path, r.status_code)


async def test_parts_bigger_than_the_json_cap_still_work(client: httpx.AsyncClient) -> None:
    size = MAX_JSON_BODY + 4096  # one part, below the 8 MiB part size
    init = await client.post(
        "/api/uploads/init", json={"filename": "big.wav", "size": size, "target_dir": ""}
    )
    assert init.status_code == 201, init.text
    sid = init.json()["session_id"]
    r = await client.put(f"/api/uploads/{sid}/parts/0", content=b"\0" * size)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ m8: Windows path shapes
def _win_inside(path: str, root: str) -> bool:
    """``resolve_in_roots``' containment on Windows path rules; ``normpath`` stands in for
    ``realpath`` (which on Windows also expands 8.3 names and follows junctions)."""
    if "\x00" in path:
        return False
    candidate = ntpath.normpath(storage._candidate(path, root, ntpath))
    return storage._is_within(
        ntpath.normcase(candidate), ntpath.normcase(ntpath.normpath(root)), ntpath
    )


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        (r"D:\Media\Uploads\a.wav", True),
        (r"d:\MEDIA\uploads\a.wav", True),  # case-insensitive
        ("d:/media/uploads/sub/a.wav", True),  # forward slashes
        (r"sub\a.wav", True),  # relative to the root
        (r"D:\Media\Uploads2\a.wav", False),  # sibling with the root as a string prefix
        (r"D:\Media", False),
        (r"C:\Media\Uploads\a.wav", False),  # another drive
        ("C:foo", False),  # drive-relative: keeps drive C:, not joined under the root
        (r"\Windows\win.ini", False),  # root-relative: the root's drive, outside the root
        (r"\\server\share\a.wav", False),  # UNC
        (r"\\?\D:\Media\Uploads\a.wav", False),  # extended prefix: fails closed without realpath
        (r"\\.\NUL", False),  # device namespace
        (r"\\?\UNC\server\share\a.wav", False),
    ],
)
def test_windows_path_shapes_against_a_drive_root(path: str, inside: bool) -> None:
    assert _win_inside(path, r"D:\Media\Uploads") is inside


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        (r"\\NAS\Media\a.wav", True),
        (r"\\nas\media\sub\a.wav", True),
        (r"\\nas\media2\a.wav", False),
        (r"\\nas\other\a.wav", False),
        (r"\\other\media\a.wav", False),
        (r"D:\a.wav", False),
    ],
)
@pytest.mark.parametrize("root", [r"\\nas\media", "\\\\nas\\media\\"])
def test_windows_path_shapes_against_a_unc_root(root: str, path: str, inside: bool) -> None:
    assert _win_inside(path, root) is inside


@pytest.mark.parametrize("path", [r"sub\..\..\x.wav", r"..\x.wav", r"D:\Media\Uploads\..\x"])
def test_backslash_dotdot_is_rejected(roots: list[Path], path: str) -> None:
    with pytest.raises(storage.PathNotAllowedError):
        storage.resolve_in_roots(path, roots)


def test_junction_escape_is_rejected(tmp_path: Path, roots: list[Path]) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    junction_or_skip(roots[0] / "escape", outside)
    with pytest.raises(storage.PathNotAllowedError):
        storage.resolve_target_dir("escape", roots)
    with pytest.raises(storage.PathNotAllowedError):
        storage.resolve_in_roots(str(roots[0] / "escape" / "a.wav"), roots)


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths are a Windows rule")
def test_root_given_in_another_case_windows(roots: list[Path]) -> None:
    (roots[0] / "Sub").mkdir()
    shouted = [Path(str(roots[0]).upper())]
    assert storage.resolve_in_roots(str(roots[0] / "sub"), shouted) == roots[0] / "Sub"


# ------------------------------------------------------------------- n1: LLM error mapping
async def _make_transcript(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> str:
    install(app, FakeProvider())
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded"})
    return str(done["transcript_id"])


@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("DashScope endpoint is not in the allow-list", 422),
        ("DashScope API key is not configured", 422),
        ("LLM request failed (HTTP 500)", 502),
    ],
)
async def test_llm_suggestions_and_summary_map_errors_alike(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    status: int,
) -> None:
    tid = await _make_transcript(app, client, env)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError(message)

    monkeypatch.setattr(transcripts_route.LlmCorrector, "suggest", fail)
    monkeypatch.setattr(transcripts_route.Summarizer, "summarize", fail)
    suggestions = await client.post(f"/api/transcripts/{tid}/llm-suggestions", json={})
    summary = await client.post(f"/api/transcripts/{tid}/summary")
    assert (suggestions.status_code, summary.status_code) == (status, status)


# ------------------------------------------------------------- n2: one containment check
def test_delete_containment_uses_the_storage_check(tmp_path: Path) -> None:
    root = (tmp_path / "uploads").resolve()
    (root / "sub").mkdir(parents=True)
    sibling = tmp_path / "uploads2"
    sibling.mkdir()
    assert jobs_route._inside_roots(root / "sub" / "a.wav", [root])
    assert not jobs_route._inside_roots(root, [root])  # the root itself is never deleted
    assert not jobs_route._inside_roots(sibling / "a.wav", [root])
    assert not jobs_route._inside_roots(tmp_path / "a.wav", [root])


# ------------------------------------------------------------------- n7: non-ASCII API key
async def test_non_ascii_api_key_matches_its_utf8_bytes() -> None:
    key = "ключ-密钥-key"
    mw = ApiKeyMiddleware(_ok, api_key=SecretStr(key))
    ok = [(b"x-api-key", key.encode("utf-8"))]
    assert await _status(mw, client=("10.0.0.2", 1), headers=ok) == 200
    wrong = [(b"x-api-key", key.encode("utf-8")[:-1])]
    assert await _status(mw, client=("10.0.0.2", 1), headers=wrong) == 401
    assert await _status(mw, client=("10.0.0.2", 1), headers=[]) == 401


def test_serve_warns_about_a_key_browsers_cannot_send(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_factory(ASR_API_KEY="ключ-密钥-key-0123456789")
    result, calls = _serve(monkeypatch, [])
    assert result.exit_code == 0, result.output
    assert calls
    assert "printable" in result.output
    env_factory(ASR_API_KEY="k" * 20)
    result, _ = _serve(monkeypatch, [])
    assert "printable" not in result.output


# Keep the uploads module imported so monkeypatching ``storage`` above affects its routes.
assert uploads_route.storage is storage
