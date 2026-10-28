"""Directory picker (PLAN §5.1). Owned by ``backend-core`` (M1).

Only directories inside ``ASR_UPLOAD_ROOTS`` are listed; ``path`` is validated exactly like an
upload ``target_dir`` (``server.storage.resolve_in_roots``) but never created.
"""

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server import storage
from server.deps import EnvDep
from server.schemas import FsDir, FsDirsOut, FsRoot

router = APIRouter(prefix="/api/fs", tags=["fs"])

MAX_DIRS = 1000


@router.get("/roots", response_model=list[FsRoot])
async def list_roots(env: EnvDep) -> list[FsRoot]:
    return [FsRoot(name=root.name or str(root), path=str(root)) for root in env.upload_roots]


def _list_dirs(path: str | None, roots: list[Path]) -> FsDirsOut:
    if not roots:
        raise storage.PathNotAllowedError("no upload roots configured")
    real = storage.resolve_in_roots(path if path else str(roots[0]), roots)
    root = storage.containing_root(real, roots)
    if root is None:  # pragma: no cover - resolve_in_roots already checked
        raise storage.PathNotAllowedError("path is outside the allowed upload roots")
    if not real.exists():
        raise storage.NotFoundError("directory not found")
    if not real.is_dir():
        raise storage.InvalidPathError("path is not a directory")

    dirs: list[FsDir] = []
    try:
        entries = list(os.scandir(real))
    except OSError as exc:
        raise storage.NotFoundError("directory is not readable") from exc
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir(follow_symlinks=True):
                continue
            # Symlinked directories must still resolve inside a root.
            if entry.is_symlink() and storage.containing_root(entry.path, roots) is None:
                continue
            if not os.access(entry.path, os.R_OK | os.X_OK):
                continue
        except OSError:
            continue
        dirs.append(FsDir(name=entry.name, path=entry.path))
    dirs.sort(key=lambda d: (d.name.casefold(), d.name))

    same_as_root = os.path.normcase(str(real)) == os.path.normcase(str(root))
    return FsDirsOut(
        path=str(real),
        parent=None if same_as_root else str(real.parent),
        root=str(root),
        dirs=dirs[:MAX_DIRS],
    )


@router.get("/dirs", response_model=FsDirsOut)
async def list_dirs(
    env: EnvDep,
    path: Annotated[
        str | None, Query(max_length=4096, description="Defaults to the first root.")
    ] = None,
) -> FsDirsOut:
    """Immediate subdirectories (hidden and unreadable ones skipped), sorted, at most 1000."""
    try:
        return await run_in_threadpool(_list_dirs, path, list(env.upload_roots))
    except storage.StorageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
