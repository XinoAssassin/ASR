"""``/api/uploads`` routes: multipart, chunked, register, get (PLAN §5.1)."""

# Blocking filesystem checks inside async tests are fine (tmp dirs, no event-loop load).
# ruff: noqa: ASYNC240

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from server import storage
from server.config import EnvSettings
from server.routes import uploads as uploads_route
from tests.conftest import API_KEY, ClientFactory
from tests.test_audio_fixtures import correlated_stereo, wav_bytes, write_audio


@pytest.fixture
def small_parts(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(storage, "PART_SIZE", 16 * 1024)
    return 16 * 1024


def _root(env: EnvSettings) -> Path:
    return env.upload_roots[0]


# ---------------------------------------------------------------------------------- multipart
async def test_multipart_upload_into_subdirectory(
    client: httpx.AsyncClient, env: EnvSettings
) -> None:
    data = wav_bytes(1.0)
    r = await client.post(
        "/api/uploads",
        files={"file": ("../会议.wav", data, "audio/wav")},
        data={"target_dir": "2026/10"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    stored = Path(body["path"])
    assert stored == _root(env) / "2026" / "10" / "会议.wav"
    assert stored.read_bytes() == data
    assert body["original_name"] == "会议.wav"
    assert body["size"] == len(data)
    probe = body["probe"]
    assert (probe["codec"], probe["sample_rate"], probe["channels"]) == ("pcm_s16le", 16000, 1)
    assert probe["channel_policy"] == "mono"
    assert probe["duration"] == pytest.approx(1.0, abs=0.01)

    got = await client.get(f"/api/uploads/{body['id']}")
    assert got.status_code == 200
    assert got.json() == body
    # temp files are moved, not left behind
    assert list((env.data_dir / storage.TMP_DIRNAME).iterdir()) == []


async def test_multipart_collision_suffix(client: httpx.AsyncClient, env: EnvSettings) -> None:
    data = wav_bytes(0.5)
    names = []
    for _ in range(2):
        r = await client.post(
            "/api/uploads", files={"file": ("a.wav", data)}, data={"target_dir": ""}
        )
        assert r.status_code == 201
        names.append(Path(r.json()["path"]).name)
    assert names == ["a.wav", "a (1).wav"]


async def test_multipart_target_dir_after_file_field(
    client: httpx.AsyncClient, env: EnvSettings
) -> None:
    boundary = "XyZ"
    data = wav_bytes(0.5)
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="b.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + data
        + (
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="target_dir"\r\n\r\n'
            f"later\r\n--{boundary}--\r\n"
        ).encode()
    )
    r = await client.post(
        "/api/uploads",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 201, r.text
    assert Path(r.json()["path"]) == _root(env) / "later" / "b.wav"


@pytest.mark.parametrize(
    ("target", "status"),
    [("../outside", 403), ("/etc", 403), ("a\x00b", 422), ("..\\x", 403)],
)
async def test_multipart_bad_target(
    client: httpx.AsyncClient, env: EnvSettings, target: str, status: int
) -> None:
    r = await client.post(
        "/api/uploads", files={"file": ("a.wav", wav_bytes(0.2))}, data={"target_dir": target}
    )
    assert r.status_code == status, r.text
    assert list((env.data_dir / storage.TMP_DIRNAME).iterdir()) == []


async def test_multipart_txt_is_415(client: httpx.AsyncClient, env: EnvSettings) -> None:
    r = await client.post(
        "/api/uploads", files={"file": ("notes.txt", b"hello")}, data={"target_dir": ""}
    )
    assert r.status_code == 415
    assert list(_root(env).iterdir()) == []


async def test_multipart_garbage_m4a_is_415_and_deleted(
    client: httpx.AsyncClient, env: EnvSettings
) -> None:
    r = await client.post(
        "/api/uploads",
        files={"file": ("fake.m4a", os.urandom(32 * 1024))},
        data={"target_dir": "x"},
    )
    assert r.status_code == 415
    assert "decodable" in r.json()["detail"]
    assert list((_root(env) / "x").iterdir()) == []


async def test_multipart_missing_fields(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/uploads", files={"file": ("a.wav", wav_bytes(0.2))})
    assert r.status_code == 422
    r = await client.post("/api/uploads", data={"target_dir": "x"}, files={"other": ("x", b"")})
    assert r.status_code == 422
    r = await client.post("/api/uploads", content=b"x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 422


async def test_multipart_over_cap_is_413(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uploads_route, "MAX_UPLOAD_SIZE", 10_000)
    r = await client.post(
        "/api/uploads", files={"file": ("big.wav", b"\0" * 50_000)}, data={"target_dir": ""}
    )
    assert r.status_code == 413
    assert list(_root(env).iterdir()) == []
    assert list((env.data_dir / storage.TMP_DIRNAME).iterdir()) == []


async def test_multipart_content_length_precheck(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uploads_route, "MAX_UPLOAD_SIZE", 10)
    monkeypatch.setattr(uploads_route, "_MULTIPART_OVERHEAD", 10)
    r = await client.post(
        "/api/uploads", files={"file": ("a.wav", b"\0" * 100)}, data={"target_dir": ""}
    )
    assert r.status_code == 413


# ------------------------------------------------------------------------------------ chunked
async def _chunked(
    client: httpx.AsyncClient, data: bytes, filename: str = "c.m4a", target_dir: str = "chunks"
) -> httpx.Response:
    r = await client.post(
        "/api/uploads/init",
        json={"filename": filename, "size": len(data), "target_dir": target_dir},
    )
    assert r.status_code == 201, r.text
    init = r.json()
    part = init["part_size"]
    # Upload out of order to prove assembly is by part number.
    for n in reversed(range(init["total_parts"])):
        chunk = data[n * part : (n + 1) * part]
        rp = await client.put(
            f"/api/uploads/{init['session_id']}/parts/{n}",
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert rp.status_code == 200, rp.text
        assert rp.json() == {"n": n, "size": len(chunk)}
    return await client.post(f"/api/uploads/{init['session_id']}/complete")


async def test_chunked_happy_path(
    client: httpx.AsyncClient, env: EnvSettings, tmp_path: Path, small_parts: int
) -> None:
    src = write_audio(tmp_path, "m4a", samples=correlated_stereo(4.0, 48_000))
    data = src.read_bytes()
    assert len(data) > 2 * small_parts
    r = await _chunked(client, data)
    assert r.status_code == 201, r.text
    body = r.json()
    stored = Path(body["path"])
    assert stored == _root(env) / "chunks" / "c.m4a"
    assert stored.read_bytes() == data
    probe = body["probe"]
    assert (probe["codec"], probe["channels"], probe["channel_policy"]) == ("aac", 2, "downmix")
    assert probe["channel_correlation"] > 0.3
    assert list((env.data_dir / storage.SESSIONS_DIRNAME).iterdir()) == []


async def test_chunked_default_part_size(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/uploads/init",
        json={"filename": "x.wav", "size": 20 * 1024 * 1024, "target_dir": ""},
    )
    assert r.json()["part_size"] == 8 * 1024 * 1024
    assert r.json()["total_parts"] == 3


async def test_chunked_idempotent_part_put(
    client: httpx.AsyncClient, env: EnvSettings, small_parts: int
) -> None:
    data = wav_bytes(1.0)  # ~32 KB → 2 parts
    r = await client.post(
        "/api/uploads/init", json={"filename": "i.wav", "size": len(data), "target_dir": ""}
    )
    sid = r.json()["session_id"]
    for _ in range(2):
        rp = await client.put(f"/api/uploads/{sid}/parts/0", content=data[:small_parts])
        assert rp.status_code == 200
    rp = await client.put(f"/api/uploads/{sid}/parts/1", content=data[small_parts:])
    assert rp.status_code == 200
    r = await client.post(f"/api/uploads/{sid}/complete")
    assert r.status_code == 201
    assert Path(r.json()["path"]).read_bytes() == data


async def test_chunked_bad_part_size(client: httpx.AsyncClient, small_parts: int) -> None:
    size = small_parts * 2 + 100
    r = await client.post(
        "/api/uploads/init", json={"filename": "a.wav", "size": size, "target_dir": ""}
    )
    sid = r.json()["session_id"]
    short = await client.put(f"/api/uploads/{sid}/parts/0", content=b"\0" * (small_parts - 1))
    assert short.status_code == 422
    long = await client.put(f"/api/uploads/{sid}/parts/0", content=b"\0" * (small_parts + 1))
    assert long.status_code == 422
    last_wrong = await client.put(f"/api/uploads/{sid}/parts/2", content=b"\0" * small_parts)
    assert last_wrong.status_code == 422
    last_ok = await client.put(f"/api/uploads/{sid}/parts/2", content=b"\0" * 100)
    assert last_ok.status_code == 200


async def test_chunked_streamed_body_without_content_length(
    client: httpx.AsyncClient, small_parts: int
) -> None:
    r = await client.post(
        "/api/uploads/init",
        json={"filename": "a.wav", "size": small_parts + 5, "target_dir": ""},
    )
    sid = r.json()["session_id"]

    async def too_much():  # type: ignore[no-untyped-def]
        for _ in range(4):
            yield b"\0" * small_parts

    rp = await client.put(f"/api/uploads/{sid}/parts/0", content=too_much())
    assert rp.status_code == 422


async def test_chunked_out_of_range_n(client: httpx.AsyncClient, small_parts: int) -> None:
    r = await client.post(
        "/api/uploads/init", json={"filename": "a.wav", "size": 10, "target_dir": ""}
    )
    sid = r.json()["session_id"]
    assert (await client.put(f"/api/uploads/{sid}/parts/1", content=b"\0" * 10)).status_code == 422
    assert (await client.put(f"/api/uploads/{sid}/parts/-1", content=b"\0")).status_code == 422


async def test_chunked_missing_parts(client: httpx.AsyncClient, small_parts: int) -> None:
    size = small_parts * 3
    r = await client.post(
        "/api/uploads/init", json={"filename": "a.wav", "size": size, "target_dir": ""}
    )
    sid = r.json()["session_id"]
    await client.put(f"/api/uploads/{sid}/parts/1", content=b"\0" * small_parts)
    rc = await client.post(f"/api/uploads/{sid}/complete")
    assert rc.status_code == 422
    assert "0, 2" in rc.json()["detail"]


async def test_chunked_over_cap(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/uploads/init",
        json={"filename": "a.wav", "size": 2 * 1024**3 + 1, "target_dir": ""},
    )
    assert r.status_code == 413


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"filename": "a.txt", "size": 10, "target_dir": ""}, 415),
        ({"filename": "a.wav", "size": 10, "target_dir": "../up"}, 403),
        ({"filename": "a.wav", "size": 10, "target_dir": "/"}, 403),
        ({"filename": "a.wav", "size": 0, "target_dir": ""}, 422),
    ],
)
async def test_chunked_init_validation(
    client: httpx.AsyncClient, payload: dict[str, object], status: int
) -> None:
    r = await client.post("/api/uploads/init", json=payload)
    assert r.status_code == status, r.text


async def test_chunked_unknown_session(client: httpx.AsyncClient) -> None:
    sid = "A" * 32
    assert (await client.put(f"/api/uploads/{sid}/parts/0", content=b"x")).status_code == 404
    assert (await client.post(f"/api/uploads/{sid}/complete")).status_code == 404
    assert (await client.post("/api/uploads/..%2F..%2Fetc/complete")).status_code == 404
    assert (await client.put("/api/uploads/short/parts/0", content=b"x")).status_code == 404


async def test_chunked_garbage_is_415_and_deleted(
    client: httpx.AsyncClient, env: EnvSettings, small_parts: int
) -> None:
    r = await _chunked(client, os.urandom(small_parts + 10), filename="g.m4a", target_dir="g")
    assert r.status_code == 415
    assert list((_root(env) / "g").iterdir()) == []


# ----------------------------------------------------------------------------------- register
async def test_register_inside_root(client: httpx.AsyncClient, env: EnvSettings) -> None:
    (_root(env) / "existing").mkdir(parents=True)
    path = _root(env) / "existing" / "memo.wav"
    path.write_bytes(wav_bytes(0.5))
    r = await client.post("/api/uploads/register", json={"path": "existing/memo.wav"})
    assert r.status_code == 201, r.text
    assert r.json()["path"] == str(path)
    assert r.json()["original_name"] == "memo.wav"
    assert r.json()["registered"] is True
    r2 = await client.post("/api/uploads/register", json={"path": str(path)})
    assert r2.status_code == 201
    assert r2.json()["id"] != r.json()["id"]


async def test_register_outside_root(
    client: httpx.AsyncClient, env: EnvSettings, tmp_path: Path
) -> None:
    outside = tmp_path / "private.wav"
    outside.write_bytes(wav_bytes(0.2))
    assert (
        await client.post("/api/uploads/register", json={"path": str(outside)})
    ).status_code == 403
    rel = "../private.wav"
    assert (await client.post("/api/uploads/register", json={"path": rel})).status_code == 403
    link = _root(env) / "link.wav"
    link.symlink_to(outside)
    assert (
        await client.post("/api/uploads/register", json={"path": "link.wav"})
    ).status_code == 403


async def test_register_errors(client: httpx.AsyncClient, env: EnvSettings) -> None:
    assert (
        await client.post("/api/uploads/register", json={"path": "nope.wav"})
    ).status_code == 404
    (_root(env) / "notes.txt").write_text("x", encoding="utf-8")
    assert (
        await client.post("/api/uploads/register", json={"path": "notes.txt"})
    ).status_code == 415
    garbage = _root(env) / "garbage.m4a"
    garbage.write_bytes(os.urandom(4096))
    r = await client.post("/api/uploads/register", json={"path": "garbage.m4a"})
    assert r.status_code == 415
    assert garbage.exists()  # registered files are never deleted


async def test_get_unknown_upload(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/uploads/doesnotexist")).status_code == 404


async def test_multistream_probe_reports_streams(
    client: httpx.AsyncClient, env: EnvSettings, tmp_path: Path
) -> None:
    from tests.test_audio_fixtures import write_multistream

    src = write_multistream(tmp_path)
    r = await client.post(
        "/api/uploads", files={"file": ("obs.mkv", src.read_bytes())}, data={"target_dir": ""}
    )
    assert r.status_code == 201
    assert r.json()["probe"]["audio_streams"] == 2


# --------------------------------------------------------------------------------------- auth
async def test_api_key_enforced(api_key_env: EnvSettings, make_client: ClientFactory) -> None:
    async with make_client(api_key_env) as anon:
        assert (await anon.post("/api/uploads/init", json={})).status_code == 401
        assert (await anon.put("/api/uploads/x/parts/0", content=b"")).status_code == 401
        assert (await anon.post("/api/uploads/register", json={"path": "x"})).status_code == 401
        assert (await anon.get("/api/uploads/x")).status_code == 401
        r = await anon.post(
            "/api/uploads", files={"file": ("a.wav", b"x")}, data={"target_dir": ""}
        )
        assert r.status_code == 401
    async with make_client(api_key_env, headers={"X-API-Key": API_KEY}) as authed:
        r = await authed.post(
            "/api/uploads", files={"file": ("a.wav", wav_bytes(0.2))}, data={"target_dir": ""}
        )
        assert r.status_code == 201
