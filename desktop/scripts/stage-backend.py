#!/usr/bin/env python3
"""Stage the backend resources of the "desktop + backend" edition.

Writes ``desktop/build/managed-backend/`` (git-ignored), which ``tauri.bundled.conf.json`` ships
as the ``managed-backend/`` resource folder next to the exe:

    managed-backend/
      source/            backend/asr, backend/server, pyproject.toml, uv.lock, .python-version
                         (no tests, data, models, .venv, __pycache__)
      uv.exe             astral-sh/uv release for x86_64-pc-windows-msvc, pinned and verified
      VERSION            <project version>+<sha256 of the staged source, 12 hex>
      README.txt         what this is, uv's licence

The uv archive is downloaded once into ``desktop/build/downloads/`` and verified against the
SHA-256 pinned here *and* the release's own ``.sha256`` file. Stdlib only; runs on Windows or
Linux / WSL. ``HTTPS_PROXY`` is honoured (urllib).

    python desktop/scripts/stage-backend.py [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import sys
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path

UV_VERSION = "0.13.1"  # 2026-10-14
UV_TARGET = "x86_64-pc-windows-msvc"
UV_SHA256 = "3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{UV_TARGET}.zip"

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
DESKTOP = REPO / "desktop"
SOURCE_DIRS = ("asr", "server")
SOURCE_FILES = ("pyproject.toml", "uv.lock", ".python-version")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def fetch(url: str, attempts: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "asr-desktop-stage"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (fixed https URL)
                return r.read()
        except OSError as exc:  # URLError, resets, timeouts
            if i == attempts - 1:
                raise
            print(f"retrying {url}: {exc}")
            time.sleep(2 * (i + 1))
    raise AssertionError("unreachable")


def stage_source(dst: Path) -> list[Path]:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    staged: list[Path] = []
    for d in SOURCE_DIRS:
        for src in sorted((BACKEND / d).rglob("*")):
            rel = src.relative_to(BACKEND)
            if any(p in SKIP_DIRS for p in rel.parts) or src.suffix in SKIP_SUFFIXES:
                continue
            if src.is_file():
                out = dst / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)
                staged.append(rel)
    for f in SOURCE_FILES:
        shutil.copyfile(BACKEND / f, dst / f)
        staged.append(Path(f))
    return staged


def source_version(dst: Path, staged: list[Path]) -> str:
    h = hashlib.sha256()
    for rel in sorted(staged, key=lambda p: p.as_posix()):
        h.update(rel.as_posix().encode())
        h.update(b"\0")
        h.update((dst / rel).read_bytes())
        h.update(b"\0")
    project = tomllib.loads((dst / "pyproject.toml").read_text(encoding="utf-8"))
    return f"{project['project']['version']}+{h.hexdigest()[:12]}"


def stage_uv(out: Path, downloads: Path) -> None:
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"uv-{UV_VERSION}-{UV_TARGET}.zip"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != UV_SHA256:
        print(f"downloading {UV_URL}")
        data = fetch(UV_URL)
        archive.write_bytes(data)
    data = archive.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    published = fetch(UV_URL + ".sha256").decode().split()[0].lower()
    if digest != UV_SHA256 or digest != published:
        archive.unlink(missing_ok=True)
        sys.exit(
            f"uv checksum mismatch: got {digest}, pinned {UV_SHA256}, release file {published}"
        )
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == "uv.exe"]
        if len(names) != 1:
            sys.exit(f"uv.exe not found in {archive.name}: {z.namelist()}")
        (out / "uv.exe").write_bytes(z.read(names[0]))
    print(f"uv {UV_VERSION} ({UV_TARGET}) verified: sha256 {digest}")


README = """ASR backend for the "desktop + backend" edition of ASR Recorder
================================================================

source/   The ASR backend (FastAPI server, core library and CLI) of this app version.
          On first run the app creates a Python 3.12 environment for it with uv
          (cpu / cuda / rocm), next to the app in portable mode, else under
          %LOCALAPPDATA%\\dev.asr.recorder\\backend.
uv.exe    uv {uv} by Astral (https://github.com/astral-sh/uv), dual-licensed under the
          MIT licence and the Apache License 2.0.
VERSION   Version of the bundled backend source: {version}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DESKTOP / "build" / "managed-backend")
    ap.add_argument("--downloads", type=Path, default=DESKTOP / "build" / "downloads")
    args = ap.parse_args()
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    staged = stage_source(out / "source")
    version = source_version(out / "source", staged)
    stage_uv(out, args.downloads.resolve())
    (out / "VERSION").write_text(version + "\n", encoding="utf-8")
    (out / "README.txt").write_text(
        README.format(uv=UV_VERSION, version=version), encoding="utf-8", newline="\r\n"
    )
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"staged {len(staged)} source files, version {version}, {size / 1e6:.1f} MB -> {out}")


if __name__ == "__main__":
    main()
