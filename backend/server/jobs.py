"""Job queue and runner. Owned by ``backend-jobs`` (M1).

Design:

- Two asyncio queues of job ids: ``cloud`` (``cloud_concurrency`` workers, default 3) and
  ``local`` (one worker: local models are CPU / GPU bound and loaded once per process).
- A worker claims a job with an atomic ``queued -> running`` transition, then runs the synchronous
  provider in a thread (``asyncio.to_thread``).
- The provider's progress callback runs in that thread. It checks the job's cancel flag (raising
  ``JobCancelled``) and hops to the event loop with ``call_soon_threadsafe``; the loop side keeps
  a live snapshot, writes it to the DB at most every ``PROGRESS_WRITE_INTERVAL`` seconds (plus
  every stage change / new external id) and publishes each write to SSE subscribers.
- Cancel: a queued job flips to ``cancelled`` immediately (atomic transition); a running job gets
  its flag set and stops at the provider's next progress call.
- Cancel after the run was released (only the ``succeeded`` / terminal write is left) waits briefly
  for that write and returns the final row instead of answering "running".
- Finishing a run (post-correction + transcript write) is ``server.finalize.TranscriptFinalizer``.
- Startup (D13): ``running`` -> failed "interrupted by restart"; ``queued`` jobs are re-enqueued;
  stale ``work/<job_id>`` dirs are removed before any worker starts. On stop the work dir of a
  run whose provider thread may still be writing is left for that cleanup.
"""

import asyncio
import contextlib
import logging
import shutil
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asr.providers
from asr.providers.base import (
    AudioSource,
    JobCancelled,
    ProgressEvent,
    ProviderError,
    Transcript,
    TranscriptionProvider,
)
from asr.providers.cloud.security import redact
from server import storage
from server.config import EffectiveSettings, EnvSettings, load_effective_settings
from server.db import Database, utcnow
from server.finalize import TranscriptFinalizer
from server.schemas import JobOut, JobStatus

log = logging.getLogger(__name__)

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({"queued", "running"})

PROGRESS_WRITE_INTERVAL = 0.5  # seconds -> at most ~2 progress writes per second per job
SUBSCRIBER_QUEUE_SIZE = 64
FINISH_WAIT = 10.0  # seconds a late cancel waits for the final write of a released run

GENERIC_FAILURE = "Internal error while running the job; see the server log."
INTERRUPTED = "interrupted by restart"
MISSING_KEY = "DashScope API key is not configured"

ProviderFactory = Callable[[JobOut, EffectiveSettings, Path], TranscriptionProvider]
"""``(job, effective settings, work dir) -> provider``. Injectable for tests."""


def default_provider_factory(
    job: JobOut, settings: EffectiveSettings, work_dir: Path
) -> TranscriptionProvider:
    if job.provider == "local":
        return asr.providers.get_provider(
            "local",
            cache_dir=settings.model_cache_dir,
            asr_model=job.options.local.asr_model,
            device=job.options.local.device or settings.local_device,
        )
    if job.provider != "cloud":
        return asr.providers.get_provider(job.provider)
    return asr.providers.get_provider(
        "cloud",
        api_key=settings.cloud_api_key.get_secret_value(),
        endpoint=settings.cloud_endpoint,
        model=job.options.cloud.model or settings.cloud_model,
        work_dir=work_dir,
        file_transfer=settings.cloud_file_transfer,
    )


def _elapsed(job: JobOut) -> float | None:
    """Wall-clock run time of a finished job, for logs."""
    if job.started_at is None or job.finished_at is None:
        return None
    return round((job.finished_at - job.started_at).total_seconds(), 1)


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


@dataclass
class _Running:
    """Loop-side state of one claimed job."""

    job: JobOut
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: bool = False
    released: bool = False
    last_write: float = 0.0
    flush_handle: asyncio.TimerHandle | None = None
    pending: set[asyncio.Task[None]] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once the run is over (terminal status written or abandoned)."""


class JobManager:
    def __init__(
        self,
        *,
        db: Database,
        env: EnvSettings,
        provider_factory: ProviderFactory | None = None,
        cloud_concurrency: int = 3,
    ) -> None:
        self.db = db
        self.env = env
        # Resolved at call time so tests may also replace ``server.jobs.default_provider_factory``.
        self.provider_factory: ProviderFactory | None = provider_factory
        self.cloud_concurrency = max(1, cloud_concurrency)
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, _Running] = {}
        # Runs past ``_settle``: no longer cancellable, final write still pending.
        self._finishing: dict[str, _Running] = {}
        self.finalizer = TranscriptFinalizer(db)
        self._subscribers: dict[str, set[asyncio.Queue[JobOut | None]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    # ------------------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Called once from the app lifespan after the database is open."""
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._queues = {"cloud": asyncio.Queue(), "local": asyncio.Queue()}
        # Nothing runs yet: every work dir is left over from a crash or an interrupted stop.
        await asyncio.to_thread(shutil.rmtree, self._work_root(), True)

        now = utcnow()
        for job in await self.db.list_jobs_by_status("running"):
            await self.db.transition_job(
                job.id,
                ("running",),
                status="failed",
                error=INTERRUPTED,
                message=None,
                finished_at=now,
            )
        for job in await self.db.list_jobs_by_status("queued"):
            self.enqueue(job)

        for i in range(self.cloud_concurrency):
            self._workers.append(
                asyncio.create_task(self._worker("cloud"), name=f"job-worker-cloud-{i}")
            )
        self._workers.append(asyncio.create_task(self._worker("local"), name="job-worker-local"))

    async def stop(self) -> None:
        """Called once from the app lifespan before the database closes.

        Running provider threads are asked to stop at their next progress call; their jobs stay
        ``running`` in the DB and are marked interrupted on the next start (D13).
        """
        self._stopping = True
        for state in self._running.values():
            state.cancel.set()
            if state.flush_handle is not None:
                state.flush_handle.cancel()
            for pending in state.pending:
                pending.cancel()
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workers.clear()
        for job_id in list(self._subscribers):
            self._publish_end(job_id)

    # ---------------------------------------------------------------------------- public
    def enqueue(self, job: JobOut) -> None:
        queue = self._queues.get(job.provider)
        if queue is None:
            raise RuntimeError("JobManager is not started")
        queue.put_nowait(job.id)

    def is_running(self, job_id: str) -> bool:
        return job_id in self._running

    async def get_state(self, job_id: str) -> JobOut | None:
        """The DB row; for a running job, overlaid with its newest live progress."""
        job = await self.db.get_job(job_id)
        state = self._running.get(job_id)
        if job is not None and state is not None and job.status == "running":
            live = state.job
            job = job.model_copy(
                update={
                    "stage": live.stage,
                    "progress": live.progress,
                    "message": live.message,
                    "cloud_task_id": live.cloud_task_id,
                }
            )
        return job

    async def cancel(self, job_id: str) -> JobOut | None:
        """Cancel a queued or running job. Returns the job (None if it does not exist)."""
        cancelled = await self.db.transition_job(
            job_id,
            ("queued",),
            status="cancelled",
            message=None,
            finished_at=utcnow(),
        )
        if cancelled is not None:
            self._publish(cancelled)
            return cancelled
        state = self._running.get(job_id)
        if state is not None and not state.finished:
            state.cancel.set()
            state.job = state.job.model_copy(update={"message": "cancelling"})
            current = await self.get_state(job_id)
            if current is not None:
                self._publish(current)
            return current
        if state is not None or job_id in self._finishing:
            # Too late to cancel: report how the run ends rather than "running".
            done = (state or self._finishing[job_id]).done
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), FINISH_WAIT)
        return await self.db.get_job(job_id)

    async def retry(self, job_id: str) -> JobOut | None:
        """``failed | cancelled -> queued`` with progress fields reset. None if not allowed."""
        job = await self.db.transition_job(
            job_id,
            ("failed", "cancelled"),
            status="queued",
            stage="queued",
            progress=0.0,
            message=None,
            error=None,
            warnings=[],
            cloud_task_id=None,
            transcript_id=None,
            started_at=None,
            finished_at=None,
        )
        if job is None:
            return None
        self._publish(job)
        self.enqueue(job)
        return job

    async def delete(self, job_id: str) -> bool:
        """Cancel the job if active, delete the row (and its transcript), end SSE streams."""
        state = self._running.get(job_id)
        if state is not None:
            state.cancel.set()
        deleted = await self.db.delete_job(job_id)
        self._publish_end(job_id)
        return deleted

    # ------------------------------------------------------------------------- pub / sub
    def subscribe(self, job_id: str) -> asyncio.Queue[JobOut | None]:
        """Queue receiving every published ``JobOut``; ``None`` means the stream should end."""
        queue: asyncio.Queue[JobOut | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[JobOut | None]) -> None:
        subs = self._subscribers.get(job_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            del self._subscribers[job_id]

    def subscriber_count(self, job_id: str) -> int:
        return len(self._subscribers.get(job_id, ()))

    def _offer(self, queue: asyncio.Queue[JobOut | None], item: JobOut | None) -> None:
        # A slow consumer loses the oldest snapshot, never the newest (or the end marker).
        while True:
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()

    def _publish(self, job: JobOut) -> None:
        for queue in list(self._subscribers.get(job.id, ())):
            self._offer(queue, job)

    def _publish_end(self, job_id: str) -> None:
        for queue in list(self._subscribers.get(job_id, ())):
            self._offer(queue, None)

    # ---------------------------------------------------------------------------- worker
    async def _worker(self, kind: str) -> None:
        queue = self._queues[kind]
        while True:
            job_id = await queue.get()
            try:
                if job_id in self._running:  # duplicate queue entry (e.g. retried)
                    continue
                await self._run(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job worker crashed on job %s", job_id)
            finally:
                queue.task_done()

    async def _run(self, job_id: str) -> None:
        # Register the cancel flag *before* the atomic queued->running transition: a concurrent
        # cancel() either wins its own queued->cancelled transition, or finds the flag here.
        placeholder = await self.db.get_job(job_id)
        if placeholder is None or placeholder.status != "queued" or job_id in self._running:
            return
        state = _Running(job=placeholder)
        self._running[job_id] = state
        work_dir = self._work_dir(job_id)
        secrets: list[str] = []
        try:
            job = await self.db.transition_job(
                job_id,
                ("queued",),
                status="running",
                started_at=utcnow(),
                message=None,
                error=None,
            )
            if job is None:  # cancelled or deleted meanwhile
                return
            state.job = job
            self._publish(job)
            log.info(
                "job %s started (provider=%s)",
                job_id,
                job.provider,
                extra={"job_id": job_id, "provider": job.provider},
            )

            settings = await load_effective_settings(self.env, self.db)
            secrets.append(settings.cloud_api_key.get_secret_value())
            try:
                transcript = await self._execute(state, settings, work_dir)
            except JobCancelled:
                await self._finish(state, status="cancelled", error=None)
                return
            except ProviderError as exc:
                if state.cancel.is_set():
                    await self._finish(state, status="cancelled", error=None)
                else:
                    await self._finish(
                        state, status="failed", error=_redact(str(exc), secrets) or "failed"
                    )
                return
            except Exception as exc:
                trace = "".join(traceback.format_exception(exc))
                trace = redact(trace, tuple(secrets), max_chars=None)
                log.error("job %s failed unexpectedly:\n%s", job_id, _redact(trace, secrets))
                await self._finish(state, status="failed", error=GENERIC_FAILURE)
                return

            if state.cancel.is_set():
                await self._finish(state, status="cancelled", error=None)
                return
            await self._succeed(state, transcript)
        finally:
            state.finished = True
            if state.flush_handle is not None:
                state.flush_handle.cancel()
            try:
                await self._release(state)
            finally:
                if self._finishing.get(job_id) is state:
                    del self._finishing[job_id]
                state.done.set()

    async def _execute(
        self, state: _Running, settings: EffectiveSettings, work_dir: Path
    ) -> Transcript:
        job = state.job
        if job.provider == "cloud" and not settings.cloud_api_key_set:
            raise ProviderError(MISSING_KEY)
        if job.upload_id is not None:
            upload = await self.db.get_upload(job.upload_id)
            if upload is None:
                raise ProviderError("the uploaded audio no longer exists")
            try:
                path = await asyncio.to_thread(
                    storage.resolve_stored_file, upload.path, self.env.upload_roots
                )
            except storage.NotFoundError:
                raise ProviderError("the uploaded audio file is missing on the server") from None
            except storage.StorageError:
                raise ProviderError(
                    "the uploaded audio file resolves outside the upload roots"
                ) from None
            source = AudioSource(path=path, probe=upload.probe)
        elif job.file_url is not None:
            source = AudioSource(url=job.file_url)
        else:
            raise ProviderError("job has no audio source")

        await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
        factory = self.provider_factory or default_provider_factory
        provider = factory(job, settings, work_dir)
        callback = self._make_callback(state)
        if state.cancel.is_set():
            raise JobCancelled
        return await asyncio.to_thread(provider.transcribe, source, job.options, callback)

    # -------------------------------------------------------------------------- progress
    def _make_callback(self, state: _Running) -> Callable[[ProgressEvent], None]:
        loop = self._loop
        assert loop is not None  # noqa: S101 - set in start()

        def progress(event: ProgressEvent) -> None:  # runs in the provider thread
            if state.cancel.is_set() or self._stopping:
                raise JobCancelled
            try:
                loop.call_soon_threadsafe(self._on_progress, state, event)
            except RuntimeError as exc:  # loop closed during shutdown
                raise JobCancelled from exc

        return progress

    def _on_progress(self, state: _Running, event: ProgressEvent) -> None:  # loop thread
        if state.finished:
            return
        prev = state.job
        update: dict[str, Any] = {
            "stage": event.stage,
            "progress": min(max(event.progress, prev.progress), 1.0),
            "message": event.message,
        }
        if event.external_id:
            update["cloud_task_id"] = event.external_id
        state.job = prev.model_copy(update=update)
        important = event.stage != prev.stage or (
            event.external_id is not None and event.external_id != prev.cloud_task_id
        )
        elapsed = time.monotonic() - state.last_write
        if important or elapsed >= PROGRESS_WRITE_INTERVAL:
            self._flush_now(state)
        elif state.flush_handle is None:
            assert self._loop is not None  # noqa: S101
            state.flush_handle = self._loop.call_later(
                PROGRESS_WRITE_INTERVAL - elapsed, self._flush_now, state
            )

    def _flush_now(self, state: _Running) -> None:
        if state.flush_handle is not None:
            state.flush_handle.cancel()
            state.flush_handle = None
        if state.finished:
            return
        state.last_write = time.monotonic()
        task = asyncio.ensure_future(self._write_progress(state, state.job))
        state.pending.add(task)
        task.add_done_callback(state.pending.discard)

    async def _write_progress(self, state: _Running, snapshot: JobOut) -> None:
        try:
            job = await self.db.transition_job(
                snapshot.id,
                ("running",),
                stage=snapshot.stage,
                progress=snapshot.progress,
                message=snapshot.message,
                cloud_task_id=snapshot.cloud_task_id,
            )
        except Exception:
            log.exception("failed to store progress of job %s", snapshot.id)
            return
        if job is not None and not state.finished:
            self._publish(job)

    async def _settle(self, state: _Running) -> None:
        """Stop progress writes and wait for in-flight ones before the final write.

        Also releases the run's bookkeeping *before* the terminal status becomes visible: a retry
        issued as soon as a client sees ``cancelled`` / ``failed`` must find the job no longer
        running, or its queue entry would be dropped as a duplicate.
        """
        state.finished = True
        self._finishing[state.job.id] = state
        if state.flush_handle is not None:
            state.flush_handle.cancel()
            state.flush_handle = None
        if state.pending:
            await asyncio.gather(*state.pending, return_exceptions=True)
        await self._release(state)

    def _work_root(self) -> Path:
        return self.env.data_dir / "work"

    def _work_dir(self, job_id: str) -> Path:
        return self._work_root() / job_id

    async def _release(self, state: _Running) -> None:
        """Forget the run and remove its work dir; idempotent, never touches a newer run.

        While stopping, the provider thread of a cancelled worker may still be writing to the
        work dir, so it is left for the cleanup in the next ``start``.
        """
        if state.released:
            return
        state.released = True
        job_id = state.job.id
        if self._running.get(job_id) is state:
            del self._running[job_id]
        if not self._stopping:
            await asyncio.to_thread(shutil.rmtree, self._work_dir(job_id), True)

    # ---------------------------------------------------------------------------- finish
    async def _finish(self, state: _Running, *, status: JobStatus, error: str | None) -> None:
        await self._settle(state)
        job = await self.db.transition_job(
            state.job.id,
            ("running",),
            status=status,
            error=error,
            message=None,
            cloud_task_id=state.job.cloud_task_id,
            finished_at=utcnow(),
        )
        if job is not None:
            self._publish(job)
            log.info(
                "job %s %s",
                job.id,
                status,
                extra={"job_id": job.id, "status": status, "elapsed_s": _elapsed(job)},
            )

    async def _succeed(self, state: _Running, transcript: Transcript) -> None:
        # Post-correction can take a while; the run stays registered meanwhile so a cancel still
        # finds its flag, and a delete is noticed (by ``store``) before anything is stored.
        result = await self.finalizer.prepare(state.job, transcript)
        if state.cancel.is_set():
            await self._finish(state, status="cancelled", error=None)
            return
        await self._settle(state)
        job_id = state.job.id
        job = await self.finalizer.store(state.job, result, cloud_task_id=state.job.cloud_task_id)
        if job is not None:
            self._publish(job)
            elapsed = _elapsed(job)
            audio_s = transcript.duration or 0.0
            log.info(
                "job %s succeeded: %d segments, %d speakers, %.1f s audio in %s s",
                job_id,
                len(result.segments),
                len(transcript.speakers),
                audio_s,
                elapsed,
                extra={
                    "job_id": job_id,
                    "status": "succeeded",
                    "elapsed_s": elapsed,
                    "audio_s": round(audio_s, 1),
                },
            )
