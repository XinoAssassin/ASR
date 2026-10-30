"""Finalize stage without the job queue (ARCH_REVIEW §4-b): ``asr.finalize`` (pure) and
``server.finalize.TranscriptFinalizer`` (DB write) against a real SQLite database."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from asr import finalize as finalize_module
from asr.finalize import CORRECTION_FAILED, finalize_transcript
from asr.providers.base import (
    CorrectionOptions,
    JobOptions,
    Segment,
    Term,
    Transcript,
    make_speakers,
)
from server.db import Database
from server.finalize import TranscriptFinalizer
from server.schemas import JobOut


def _transcript(*texts: str) -> Transcript:
    segments = [
        Segment(id=f"s{i}", speaker_id="SPEAKER_00", start=float(i), end=i + 1.0, text=t)
        for i, t in enumerate(texts)
    ]
    return Transcript(
        provider="local",
        duration=float(len(texts)),
        language="zh",
        speakers=make_speakers(s.speaker_id for s in segments),
        segments=segments,
        warnings=["provider warning"],
    )


def _options(**correction: Any) -> JobOptions:
    return JobOptions(
        provider="local",
        terms=[Term(term="Townfall", aliases=["汤泡"])],
        correction=CorrectionOptions(**correction),
    )


def _boom(*_: object, **__: object) -> None:
    raise RuntimeError("corrector exploded")


# ------------------------------------------------------------------------------- core (pure)
def test_corrects_and_keeps_the_raw_text_as_original() -> None:
    raw = _transcript("汤泡我确实不是很想玩", "今天天气不错")
    result = finalize_transcript(raw, _options())
    assert [s.text for s in result.segments] == ["Townfall我确实不是很想玩", "今天天气不错"]
    assert [s.text for s in result.original] == ["汤泡我确实不是很想玩", "今天天气不错"]
    (c,) = result.corrections
    assert (c.segment_id, c.before, c.after, c.offset, c.status) == (
        "s0",
        "汤泡",
        "Townfall",
        0,
        "applied",
    )
    assert result.warnings == ["provider warning"]
    assert result.transcript.speakers == raw.speakers and result.transcript.duration == 2.0
    # The input is untouched.
    assert raw.segments[0].text == "汤泡我确实不是很想玩" and raw.warnings == ["provider warning"]


def test_disabled_correction_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finalize_module, "correct_segments", _boom)  # must not be called
    raw = _transcript("汤泡")
    result = finalize_transcript(raw, _options(terms=False, repetition=False))
    assert result.segments == raw.segments == result.original
    assert result.corrections == [] and result.warnings == ["provider warning"]


def test_correction_failure_keeps_raw_text_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(finalize_module, "correct_segments", _boom)
    raw = _transcript("汤泡")
    with caplog.at_level(logging.ERROR, logger="asr.finalize"):
        result = finalize_transcript(raw, _options(), label="job j1")
    assert [s.text for s in result.segments] == ["汤泡"]
    assert result.original == raw.segments and result.corrections == []
    assert result.warnings == ["provider warning", CORRECTION_FAILED]
    assert raw.warnings == ["provider warning"]  # not mutated
    assert any("post-correction failed for job j1" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- server finalizer
@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "asr.db")
    try:
        yield database
    finally:
        await database.close()


async def _job(db: Database, status: str = "running") -> JobOut:
    job = JobOut(
        id="j1",
        upload_id=None,
        file_url="https://example.com/a.wav",
        provider="local",
        options=_options(),
        status=status,  # type: ignore[arg-type]
        stage="finalizing",
        progress=0.9,
        created_at=datetime.now(UTC),
    )
    await db.insert_job(job)
    return job


async def test_store_writes_transcript_and_succeeds(db: Database) -> None:
    job = await _job(db)
    finalizer = TranscriptFinalizer(db)
    result = await finalizer.prepare(job, _transcript("汤泡我确实不是很想玩"))
    done = await finalizer.store(job, result, cloud_task_id="task-1")
    assert done is not None
    assert (done.status, done.stage, done.progress) == ("succeeded", "done", 1.0)
    assert done.warnings == ["provider warning"] and done.cloud_task_id == "task-1"
    assert done.finished_at is not None and done.transcript_id
    stored = await db.get_transcript(done.transcript_id)
    assert stored is not None and stored.job_id == "j1"
    assert stored.segments[0].text == "Townfall我确实不是很想玩"
    assert stored.original[0].text == "汤泡我确实不是很想玩"
    assert [c.after for c in stored.corrections] == ["Townfall"]


async def test_store_failure_warning_reaches_job_and_transcript(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(finalize_module, "correct_segments", _boom)
    job = await _job(db)
    finalizer = TranscriptFinalizer(db)
    done = await finalizer.store(job, await finalizer.prepare(job, _transcript("汤泡")))
    assert done is not None and done.status == "succeeded"
    assert done.warnings == ["provider warning", CORRECTION_FAILED]
    stored = await db.get_transcript(done.transcript_id or "")
    assert stored is not None and stored.segments[0].text == "汤泡"
    assert CORRECTION_FAILED in stored.warnings


async def test_store_is_quiet_when_the_job_vanished(db: Database) -> None:
    job = await _job(db)
    finalizer = TranscriptFinalizer(db)
    result = await finalizer.prepare(job, _transcript("你好"))
    await db.delete_job(job.id)
    assert await finalizer.store(job, result) is None


@pytest.mark.parametrize("status", ["cancelled", "failed", "queued"])
async def test_store_writes_nothing_unless_running(db: Database, status: str) -> None:
    job = await _job(db, status)
    finalizer = TranscriptFinalizer(db)
    assert await finalizer.store(job, await finalizer.prepare(job, _transcript("你好"))) is None
    current = await db.get_job(job.id)
    assert current is not None and current.status == status and current.transcript_id is None


async def test_store_delete_between_check_and_insert(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await _job(db)
    real_insert = db.insert_transcript

    async def delete_then_insert(transcript_id: str, job_id: str, *a: Any, **kw: Any) -> Any:
        await db.delete_job(job_id)
        return await real_insert(transcript_id, job_id, *a, **kw)

    monkeypatch.setattr(db, "insert_transcript", delete_then_insert)
    finalizer = TranscriptFinalizer(db)
    assert await finalizer.store(job, await finalizer.prepare(job, _transcript("你好"))) is None


async def test_store_reraises_other_integrity_errors(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await _job(db)

    async def broken_insert(*_: Any, **__: Any) -> Any:
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(db, "insert_transcript", broken_insert)
    finalizer = TranscriptFinalizer(db)
    with pytest.raises(sqlite3.IntegrityError):
        await finalizer.store(job, await finalizer.prepare(job, _transcript("你好")))
