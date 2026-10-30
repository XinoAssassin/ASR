"""Jobs (PLAN §5.2). Owned by ``backend-jobs`` (M1)."""

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sse_starlette import EventSourceResponse

from asr.providers.base import JobOptions, LocalOptions, ProviderName, Term
from asr.providers.local import models as local_models
from asr.providers.local.pipeline import default_asr_model
from asr.terms import merge_terms
from server import storage
from server.config import EffectiveSettings, EnvSettings, load_effective_settings
from server.db import Database, utcnow
from server.deps import DbDep, EnvDep, JobManagerDep
from server.jobs import MISSING_KEY, TERMINAL_STATUSES, JobManager
from server.schemas import JobCreate, JobList, JobOut, JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
log = logging.getLogger(__name__)

MAX_FILE_URL_LENGTH = 2048
MAX_JOB_TERMS = 500
SSE_PING_SECONDS = 15

_422 = status.HTTP_422_UNPROCESSABLE_CONTENT


def _check_file_url(url: str) -> None:
    """D3: public http(s) only, no credentials. The backend never fetches it."""
    if len(url) > MAX_FILE_URL_LENGTH:
        raise HTTPException(_422, f"file_url must be at most {MAX_FILE_URL_LENGTH} characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(_422, "file_url must be an absolute http(s) URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise HTTPException(_422, "file_url must not contain credentials")
    if any(c.isspace() for c in url):
        raise HTTPException(_422, "file_url must not contain whitespace")


def _resolve_local(body: JobCreate, settings: EffectiveSettings) -> LocalOptions:
    """Validate a local job and pin its ASR model / device into the stored options."""
    if body.file_url is not None:
        raise HTTPException(_422, "the local provider needs an uploaded file, not file_url (D3)")
    if not local_models.local_stack_installed():
        raise HTTPException(
            _422, "the local model stack is not installed (uv sync --extra cpu --extra local)"
        )
    device = body.options.local.device or settings.local_device
    asr_model = (
        body.options.local.asr_model or settings.local_asr_model or default_asr_model(device)
    )
    try:
        missing = local_models.missing_models(settings.model_cache_dir, asr_model)
    except KeyError:
        raise HTTPException(_422, f"unknown local ASR model {asr_model!r}") from None
    if not local_models.engine_installed(asr_model):
        extra = local_models.ENGINE_PACKAGES[local_models.spec(asr_model).engine][1]
        raise HTTPException(
            _422, f"{asr_model} needs the `{extra}` extra (uv sync ... --extra {extra})"
        )
    if missing:
        raise HTTPException(
            _422,
            f"local models not downloaded: {', '.join(missing)} "
            f"(run `asr models download {' '.join(missing)}`)",
        )
    return body.options.local.model_copy(update={"asr_model": asr_model, "device": device})


def _effective_provider(body: JobCreate, default: ProviderName) -> ProviderName:
    """D17: ``JobCreate.provider`` -> ``options.provider`` (if set by the client) -> default."""
    if body.provider is not None:
        return body.provider
    if "provider" in body.options.model_fields_set:
        return body.options.provider
    return default


async def _expand_terms(db: Database, body: JobCreate) -> list[Term]:
    collected: list[Term] = []
    for term_set_id in dict.fromkeys(body.term_set_ids):
        term_set = await db.get_term_set(term_set_id)
        if term_set is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"term set {term_set_id!r} not found")
        collected.extend(term_set.terms)
    collected.extend(body.extra_terms)
    terms = merge_terms(collected)
    if len(terms) > MAX_JOB_TERMS:
        raise HTTPException(
            _422, f"a job accepts at most {MAX_JOB_TERMS} distinct terms (got {len(terms)})"
        )
    return terms


async def _get_or_404(db: Database, job_id: str) -> JobOut:
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreate, db: DbDep, env: EnvDep, manager: JobManagerDep) -> JobOut:
    settings = await load_effective_settings(env, db)
    provider = _effective_provider(body, settings.provider_default)
    options_update: dict[str, Any] = {}
    if provider == "local":
        options_update["local"] = _resolve_local(body, settings)
    else:
        if body.file_url is not None:
            _check_file_url(body.file_url)
        if not settings.cloud_api_key_set:
            raise HTTPException(_422, MISSING_KEY)
    if body.upload_id is not None and await db.get_upload(body.upload_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")

    terms = await _expand_terms(db, body)
    options: JobOptions = body.options.model_copy(
        update={"provider": provider, "terms": terms, **options_update}
    )
    # Re-validate the merged model so the stored options are exactly what the schema allows.
    options = JobOptions.model_validate(options.model_dump())

    job = JobOut(
        id=uuid.uuid4().hex,
        upload_id=body.upload_id,
        file_url=body.file_url,
        provider=provider,
        options=options,
        status="queued",
        stage="queued",
        progress=0.0,
        created_at=utcnow(),
    )
    await db.insert_job(job)
    manager.enqueue(job)
    return job


@router.get("", response_model=JobList)
async def list_jobs(
    db: DbDep,
    status_: Annotated[JobStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JobList:
    items, total = await db.list_jobs(
        status=status_, limit=page_size, offset=(page - 1) * page_size
    )
    return JobList(items=items, total=total, page=page, page_size=page_size)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, manager: JobManagerDep) -> JobOut:
    job = await manager.get_state(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.get(
    "/{job_id}/events",
    response_class=Response,
    responses={
        200: {
            "description": (
                "Server-sent events: `event: job`, `data: JobOut` (JSON) on every change; "
                "the stream ends after a terminal status."
            ),
            "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/JobOut"}}},
        }
    },
)
async def job_events(job_id: str, request: Request, manager: JobManagerDep) -> Response:
    if await manager.get_state(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")

    async def stream() -> AsyncIterator[dict[str, Any]]:
        # Subscribe inside the generator, next to the ``finally`` that unsubscribes: Python never
        # runs the ``finally`` of an async generator that was never started, so a subscription
        # made in the handler leaked whenever the response was torn down before streaming.
        # Subscribing before reading the state means no change between the two is lost.
        queue = manager.subscribe(job_id)
        try:
            initial = await manager.get_state(job_id)
            if initial is None:  # deleted after the 404 check
                return
            yield {"event": "job", "data": initial.model_dump_json()}
            if initial.status in TERMINAL_STATUSES:
                return
            while True:
                item = await queue.get()
                if item is None:  # job deleted / server stopping
                    return
                yield {"event": "job", "data": item.model_dump_json()}
                if item.status in TERMINAL_STATUSES:
                    return
        finally:
            manager.unsubscribe(job_id, queue)

    return EventSourceResponse(
        stream(),
        ping=SSE_PING_SECONDS,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, db: DbDep, manager: JobManagerDep) -> JobOut:
    job = await _get_or_404(db, job_id)
    if job.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is already {job.status}")
    result = await manager.cancel(job_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return result


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str, db: DbDep, manager: JobManagerDep) -> JobOut:
    job = await _get_or_404(db, job_id)
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"only failed or cancelled jobs can be retried ({job.status})"
        )
    result = await manager.retry(job_id)
    if result is None:  # changed status concurrently
        raise HTTPException(status.HTTP_409_CONFLICT, "job can no longer be retried")
    return result


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    """Strictly inside a root (rule 2, the same check as ``storage``; a root itself is not)."""
    try:
        root = storage.containing_root(path, roots)
        return root is not None and Path(os.path.realpath(path)) != root
    except OSError:
        return False


UNLINK_ATTEMPTS = 5
UNLINK_RETRY_S = 0.2
RUN_STOP_WAIT_S = 5.0


def _unlink(path: Path, what: str, upload_id: str) -> bool:
    """Remove ``path``; ``False`` (logged) if it stays. Windows refuses to delete a file that is
    still open (a provider thread reading it, ``/api/audio`` streaming the MP3 cache), so a
    ``PermissionError`` is retried briefly before giving up."""
    for attempt in range(UNLINK_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if attempt + 1 < UNLINK_ATTEMPTS:
                time.sleep(UNLINK_RETRY_S)
        except OSError:
            break
    log.warning(
        "could not delete the %s of upload %s; it is in use or not removable", what, upload_id
    )
    return False


def _remove_audio_files(
    env: EnvSettings, upload_id: str, stored_path: str, *, registered: bool
) -> bool:
    """Blocking file removal (run in a thread). Registered files belong to the user and stay.

    Returns ``False`` when the stored file could not be deleted: the caller then keeps the
    upload row, so the record still points at the file instead of orphaning it. The MP3 cache
    is derived data; a cache file that cannot be removed is only logged."""
    cache_dir = env.data_dir / "cache" / "audio"
    cached = [cache_dir / f"{upload_id}.mp3"]
    with contextlib.suppress(OSError):
        cached += list(cache_dir.glob(f"{upload_id}.s*.mp3"))  # other audio streams
    for path in cached:
        _unlink(path, "audio cache", upload_id)
    if registered:
        return True
    path = Path(stored_path)
    if _inside_roots(path, env.upload_roots) and path.is_file():
        return _unlink(path, "stored file", upload_id)
    log.warning("not deleting upload %s: stored file is outside the upload roots", upload_id)
    return True


async def _wait_until_stopped(manager: JobManager, job_id: str) -> None:
    """Bounded wait for a cancelled run to let go of its input file (Windows cannot delete an
    open file)."""
    deadline = time.monotonic() + RUN_STOP_WAIT_S
    # JobManager has no public "run released" event; poll its public state instead.
    while manager.is_running(job_id) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_job(
    job_id: str,
    db: DbDep,
    env: EnvDep,
    manager: JobManagerDep,
    delete_audio: Annotated[bool, Query(description="Also delete the uploaded file.")] = False,
) -> Response:
    """Cancels the job if active. With ``delete_audio`` the upload (record, stored file, browser
    MP3 cache) is removed too, unless another job still references it. A running job is given
    a few seconds to stop first; a stored file that still cannot be deleted (Windows: open
    elsewhere) is kept together with its upload record, and the job is deleted regardless."""
    job = await _get_or_404(db, job_id)
    await manager.delete(job_id)
    if delete_audio and job.upload_id is not None:
        upload = await db.get_upload(job.upload_id)
        if upload is not None and await db.count_jobs_for_upload(upload.id) == 0:
            await _wait_until_stopped(manager, job_id)
            removed = await asyncio.to_thread(
                _remove_audio_files, env, upload.id, upload.path, registered=upload.registered
            )
            if removed:
                await db.delete_upload(upload.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
