"""Finalize a finished job: core post-processing plus the transcript write (ARCH_REVIEW §4-b).

``TranscriptFinalizer`` is where provider output meets post-correction and the database, for
every provider. It knows nothing about the job queue, SSE or cancel flags: ``JobManager`` calls
``prepare`` while the run is still registered (so a cancel during a slow correction still finds
its flag), re-checks cancel, releases the run, then calls ``store``. Ordering and race handling:

- ``prepare`` runs ``asr.finalize.finalize_transcript`` in a thread; a correction failure keeps
  the raw text and adds a warning, it never fails the job;
- ``store`` re-reads the row and writes nothing if the job was deleted or left ``running``
  meanwhile (returns ``None``, quietly); a delete between that check and the insert surfaces as
  an ``IntegrityError`` and is treated the same way;
- otherwise it inserts the transcript (``original`` = raw provider segments, immutable) and moves
  the job ``running -> succeeded`` with the transcript's warnings.
"""

import asyncio
import sqlite3
import uuid

from asr.finalize import FinalizedTranscript, finalize_transcript
from asr.providers.base import Transcript
from server.db import Database, utcnow
from server.schemas import JobOut


class TranscriptFinalizer:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def prepare(self, job: JobOut, transcript: Transcript) -> FinalizedTranscript:
        """Post-correct ``transcript`` per the job's options (in a worker thread)."""
        return await asyncio.to_thread(
            finalize_transcript, transcript, job.options, label=f"job {job.id}"
        )

    async def store(
        self, job: JobOut, result: FinalizedTranscript, *, cloud_task_id: str | None = None
    ) -> JobOut | None:
        """Persist ``result`` and mark the job succeeded.

        Returns the updated job, or ``None`` when the job vanished or is no longer ``running``
        (nothing is written then).
        """
        current = await self.db.get_job(job.id)
        if current is None or current.status != "running":  # deleted / finished meanwhile
            return None
        transcript_id = uuid.uuid4().hex
        try:
            await self.db.insert_transcript(
                transcript_id,
                job.id,
                result.transcript,
                original=result.original,
                corrections=result.corrections,
            )
        except sqlite3.IntegrityError:
            if await self.db.get_job(job.id) is None:  # deleted between the check and the insert
                return None
            raise
        return await self.db.transition_job(
            job.id,
            ("running",),
            status="succeeded",
            stage="done",
            progress=1.0,
            message=None,
            error=None,
            warnings=list(result.warnings),
            cloud_task_id=cloud_task_id,
            transcript_id=transcript_id,
            finished_at=utcnow(),
        )
