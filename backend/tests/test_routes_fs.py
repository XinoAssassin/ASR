"""``/api/fs`` directory picker (PLAN §5.1)."""

# Blocking filesystem checks inside async tests are fine (tmp dirs, no event-loop load).
# ruff: noqa: ASYNC240

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from server.config import EnvSettings
from server.routes import fs as fs_route
from tests.conftest import API_KEY, ClientFactory, EnvFactory
from tests.fs_helpers import symlink_or_skip


@pytest.fixture
def env(env_factory: EnvFactory, tmp_path: Path) -> EnvSettings:
    roots = [tmp_path / "uploads", tmp_path / "media"]
    return env_factory(ASR_UPLOAD_ROOTS=os.pathsep.join(str(r) for r in roots))


async def test_roots(client: httpx.AsyncClient, env: EnvSettings) -> None:
    r = await client.get("/api/fs/roots")
    assert r.status_code == 200
    assert r.json() == [{"name": p.name, "path": str(p)} for p in env.upload_roots]


async def test_dirs_default_is_first_root(client: httpx.AsyncClient, env: EnvSettings) -> None:
    root = env.upload_roots[0]
    for name in ("b", "A", "c", ".hidden"):
        (root / name).mkdir()
    (root / "file.m4a").write_bytes(b"x")
    r = await client.get("/api/fs/dirs")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(root)
    assert body["root"] == str(root)
    assert body["parent"] is None
    assert [d["name"] for d in body["dirs"]] == ["A", "b", "c"]
    assert body["dirs"][0]["path"] == str(root / "A")


async def test_dirs_subdirectory_and_parent(client: httpx.AsyncClient, env: EnvSettings) -> None:
    root = env.upload_roots[1]
    (root / "x" / "y" / "z").mkdir(parents=True)
    r = await client.get("/api/fs/dirs", params={"path": str(root / "x" / "y")})
    body = r.json()
    assert body["path"] == str(root / "x" / "y")
    assert body["parent"] == str(root / "x")
    assert body["root"] == str(root)
    assert [d["name"] for d in body["dirs"]] == ["z"]
    # relative paths are relative to the first root
    (env.upload_roots[0] / "rel").mkdir()
    r = await client.get("/api/fs/dirs", params={"path": "rel"})
    assert r.json()["parent"] == str(env.upload_roots[0])


@pytest.mark.parametrize("path", ["..", "../..", "/", "/etc", "x/../../..", "..\\.."])
async def test_dirs_traversal_rejected(client: httpx.AsyncClient, path: str) -> None:
    r = await client.get("/api/fs/dirs", params={"path": path})
    assert r.status_code == 403


async def test_dirs_sibling_prefix_and_symlinks(
    client: httpx.AsyncClient, env: EnvSettings, tmp_path: Path
) -> None:
    root = env.upload_roots[0]
    sibling = Path(str(root) + "2")
    sibling.mkdir()
    assert (await client.get("/api/fs/dirs", params={"path": str(sibling)})).status_code == 403
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(root / "escape", outside, target_is_directory=True)
    symlink_or_skip(root / "to-media", env.upload_roots[1], target_is_directory=True)
    names = [d["name"] for d in (await client.get("/api/fs/dirs")).json()["dirs"]]
    assert "escape" not in names
    assert "to-media" in names
    r = await client.get("/api/fs/dirs", params={"path": "escape"})
    assert r.status_code == 403


async def test_dirs_missing_and_not_dir(client: httpx.AsyncClient, env: EnvSettings) -> None:
    assert (await client.get("/api/fs/dirs", params={"path": "missing"})).status_code == 404
    (env.upload_roots[0] / "f.wav").write_bytes(b"x")
    assert (await client.get("/api/fs/dirs", params={"path": "f.wav"})).status_code == 422
    assert (await client.get("/api/fs/dirs", params={"path": "a\x00"})).status_code == 422
    # listing never creates anything
    assert not (env.upload_roots[0] / "missing").exists()


async def test_dirs_capped(
    client: httpx.AsyncClient, env: EnvSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_route, "MAX_DIRS", 5)
    for i in range(8):
        (env.upload_roots[0] / f"d{i}").mkdir()
    dirs = (await client.get("/api/fs/dirs")).json()["dirs"]
    assert [d["name"] for d in dirs] == [f"d{i}" for i in range(5)]


async def test_api_key_enforced(api_key_env: EnvSettings, make_client: ClientFactory) -> None:
    async with make_client(api_key_env) as anon:
        assert (await anon.get("/api/fs/roots")).status_code == 401
        assert (await anon.get("/api/fs/dirs")).status_code == 401
    async with make_client(api_key_env, headers={"X-API-Key": API_KEY}) as authed:
        assert (await authed.get("/api/fs/roots")).status_code == 200
        assert (await authed.get("/api/fs/dirs")).status_code == 200
