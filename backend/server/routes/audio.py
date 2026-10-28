"""Browser playback audio (EXECUTION_LOG D12). Owned by ``backend-core`` (M1).

The first request for an upload transcodes it to MP3 under ``ASR_DATA_DIR/cache/audio`` (once,
even with concurrent requests); every request is then served by ``FileResponse``, which answers
``Range`` requests with ``206 Partial Content``.
"""

import asyncio
import re
import weakref
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from asr.audio.decode import AudioDecodeError
from asr.audio.transcode import transcode_for_browser
from server.deps import DbDep, EnvDep

router = APIRouter(prefix="/api/audio", tags=["audio"])

CACHE_SUBDIR = Path("cache") / "audio"
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# One lock per upload id while a transcode may be running; entries vanish when unused.
_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _lock_for(upload_id: str) -> asyncio.Lock:
    lock = _locks.get(upload_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[upload_id] = lock
    return lock


def _is_fresh(cache: Path, source: Path) -> bool:
    try:
        return cache.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


@router.get(
    "/{upload_id}",
    response_class=FileResponse,
    responses={
        200: {"description": "MP3 audio.", "content": {"audio/mpeg": {}}},
        206: {"description": "Partial content (Range).", "content": {"audio/mpeg": {}}},
    },
)
async def get_audio(upload_id: str, env: EnvDep, db: DbDep) -> FileResponse:
    """Cached MP3 transcode under ``ASR_DATA_DIR/cache/audio``; honours ``Range``."""
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload")
    upload = await db.get_upload(upload_id)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload")
    source = Path(upload.path)
    if not await run_in_threadpool(source.is_file):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audio file no longer exists")

    cache = env.data_dir / CACHE_SUBDIR / f"{upload_id}.mp3"
    if not await run_in_threadpool(_is_fresh, cache, source):
        lock = _lock_for(upload_id)
        async with lock:
            if not await run_in_threadpool(_is_fresh, cache, source):
                try:
                    await run_in_threadpool(transcode_for_browser, source, cache)
                except AudioDecodeError as exc:
                    raise HTTPException(
                        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"cannot decode audio: {exc}"
                    ) from exc
    return FileResponse(cache, media_type="audio/mpeg")
