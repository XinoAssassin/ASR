"""Browser playback audio (EXECUTION_LOG D12). Owned by ``backend-core`` (M1).

The first request for an upload transcodes it to MP3 under ``ASR_DATA_DIR/cache/audio`` (once,
even with concurrent requests); every request is then served by ``FileResponse``, which answers
``Range`` requests with ``206 Partial Content``.

Multi-track files play the audio stream the transcript was made from: ``?stream_index=`` or, by
default, the newest job's ``options.preprocess.stream_index`` (one cached MP3 per stream).
"""

import asyncio
import re
import weakref
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from asr.audio.decode import AudioDecodeError
from asr.audio.transcode import transcode_for_browser
from server import storage
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
async def get_audio(
    upload_id: str,
    env: EnvDep,
    db: DbDep,
    stream_index: Annotated[
        int | None,
        Query(
            ge=0,
            le=255,
            description="Audio stream to play; default: the newest job's "
            "`options.preprocess.stream_index` for this upload, else 0.",
        ),
    ] = None,
) -> FileResponse:
    """Cached MP3 transcode under ``ASR_DATA_DIR/cache/audio``; honours ``Range``."""
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload")
    upload = await db.get_upload(upload_id)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload")
    if stream_index is None:
        job = await db.latest_job_for_upload(upload_id)
        stream_index = job.options.preprocess.stream_index if job is not None else 0
    streams = upload.probe.audio_streams if upload.probe is not None else 1
    if stream_index >= max(streams, 1):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"stream_index {stream_index} out of range (the file has {streams} audio streams)",
        )
    try:
        source = await run_in_threadpool(storage.resolve_stored_file, upload.path, env.upload_roots)
    except storage.StorageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    name = f"{upload_id}.mp3" if stream_index == 0 else f"{upload_id}.s{stream_index}.mp3"
    cache = env.data_dir / CACHE_SUBDIR / name
    if not await run_in_threadpool(_is_fresh, cache, source):
        lock = _lock_for(name)
        async with lock:
            if not await run_in_threadpool(_is_fresh, cache, source):
                try:
                    await run_in_threadpool(
                        transcode_for_browser, source, cache, stream_index=stream_index
                    )
                except AudioDecodeError as exc:
                    raise HTTPException(
                        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"cannot decode audio: {exc}"
                    ) from exc
    return FileResponse(cache, media_type="audio/mpeg")
