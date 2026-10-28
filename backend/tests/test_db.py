"""Repository round-trips (``server.db``)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from asr.providers.base import JobOptions, Segment, Speaker, Term, Transcript
from server.db import SCHEMA_VERSION, Database
from server.schemas import JobOut, UploadOut


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "db" / "asr.db")
    try:
        yield database
    finally:
        await database.close()


def _job(job_id: str, *, upload_id: str | None = None, minutes: int = 0, **kw: object) -> JobOut:
    data: dict[str, object] = {
        "id": job_id,
        "upload_id": upload_id,
        "file_url": None if upload_id else "https://example.com/a.mp3",
        "provider": "cloud",
        "options": JobOptions(terms=[Term(term="芯片", weight=5)], num_speakers=3),
        "status": "queued",
        "stage": "queued",
        "progress": 0.0,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes),
    }
    data.update(kw)
    return JobOut.model_validate(data)


async def _upload(db: Database, upload_id: str = "u1") -> UploadOut:
    upload = UploadOut(
        id=upload_id,
        path="/x/会议.m4a",
        original_name="会议.m4a",
        size=10,
        created_at=datetime.now(UTC),
    )
    await db.insert_upload(upload)
    return upload


async def test_schema_version_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "asr.db"
    db = await Database.open(path)
    assert await db.schema_version() == SCHEMA_VERSION
    assert await db.journal_mode() in {"wal", "delete", "truncate", "persist", "memory"}
    await db.close()
    db = await Database.open(path)  # idempotent
    assert await db.schema_version() == SCHEMA_VERSION
    async with db.conn.execute("SELECT COUNT(*) FROM schema_version") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == 1
    await db.close()


async def test_job_round_trip(db: Database) -> None:
    await _upload(db)
    job = _job("j1", upload_id="u1", warnings=["w"])
    await db.insert_job(job)
    assert await db.get_job("j1") == job
    assert await db.get_job("missing") is None

    started = datetime(2026, 1, 2, tzinfo=UTC)
    updated = await db.update_job(
        "j1", status="running", stage="uploading", progress=0.2, started_at=started
    )
    assert updated is not None
    assert updated.status == "running" and updated.stage == "uploading"
    assert updated.started_at == started
    assert updated.options == job.options

    with pytest.raises(ValueError, match="unknown job field"):
        await db.update_job("j1", id="x")


async def test_transition_job_is_conditional(db: Database) -> None:
    await db.insert_job(_job("j1"))
    assert await db.transition_job("j1", ("running",), status="failed") is None
    moved = await db.transition_job("j1", ("queued",), status="running")
    assert moved is not None and moved.status == "running"
    assert await db.transition_job("j1", ("queued",), status="cancelled") is None
    assert await db.transition_job("missing", ("queued",), status="running") is None


async def test_list_jobs(db: Database) -> None:
    for i in range(5):
        await db.insert_job(_job(f"j{i}", minutes=i, status="failed" if i % 2 else "queued"))
    items, total = await db.list_jobs(limit=2, offset=0)
    assert total == 5 and [j.id for j in items] == ["j4", "j3"]
    items, total = await db.list_jobs(limit=2, offset=4)
    assert [j.id for j in items] == ["j0"]
    items, total = await db.list_jobs(status="failed")
    assert total == 2 and {j.id for j in items} == {"j1", "j3"}
    queued = await db.list_jobs_by_status("queued")
    assert [j.id for j in queued] == ["j0", "j2", "j4"]  # oldest first
    assert len(await db.list_jobs_by_status("queued", "failed")) == 5


async def test_count_and_delete(db: Database) -> None:
    await _upload(db)
    await db.insert_job(_job("a", upload_id="u1"))
    await db.insert_job(_job("b", upload_id="u1"))
    assert await db.count_jobs_for_upload("u1") == 2
    assert await db.count_jobs_for_upload("u1", exclude_job_id="a") == 1
    assert await db.delete_job("a") is True
    assert await db.delete_job("a") is False
    assert await db.count_jobs_for_upload("u1") == 1
    # Deleting the upload nulls the reference.
    assert await db.delete_upload("u1") is True
    job = await db.get_job("b")
    assert job is not None and job.upload_id is None


async def test_transcript_round_trip_and_revert(db: Database) -> None:
    await db.insert_job(_job("j1"))
    segments = [
        Segment(id="s0", speaker_id="SPEAKER_00", start=0.0, end=1.5, text="你好"),
        Segment(id="s1", speaker_id="SPEAKER_01", start=1.5, end=3.0, text="hello"),
    ]
    tr = Transcript(
        provider="cloud",
        duration=3.0,
        language="zh",
        speakers=[
            Speaker(id="SPEAKER_00", name="SPEAKER_00", color="#1f77b4"),
            Speaker(id="SPEAKER_01", name="SPEAKER_01", color="#ff7f0e"),
        ],
        segments=segments,
        warnings=["w1"],
    )
    out = await db.insert_transcript("t1", "j1", tr)
    assert out.segments == out.original == segments
    assert out.speakers == tr.speakers and out.warnings == ["w1"] and out.job_id == "j1"
    assert await db.get_transcript("t1") == out

    edited = [segments[0].model_copy(update={"text": "改", "edited": True}), segments[1]]
    renamed = [tr.speakers[0].model_copy(update={"name": "张三"}), tr.speakers[1]]
    up = await db.update_transcript("t1", segments=edited, speakers=renamed)
    assert up is not None
    assert up.segments == edited and up.original == segments and up.speakers == renamed
    assert up.updated_at >= out.updated_at

    reverted = await db.revert_transcript("t1")
    assert reverted is not None
    assert reverted.segments == segments and reverted.speakers == renamed
    assert await db.update_transcript("missing", segments=[]) is None
    assert await db.revert_transcript("missing") is None

    # Deleting the job cascades to its transcript.
    await db.delete_job("j1")
    assert await db.get_transcript("t1") is None


async def test_term_sets(db: Database) -> None:
    terms = [Term(term="FPGA", weight=4, note="n"), Term(term="芯片")]
    created = await db.insert_term_set("ts1", "Chips", terms)
    assert created.terms == terms and created.name == "Chips"
    await db.insert_term_set("ts2", "alpha", [])
    assert [t.id for t in await db.list_term_sets()] == ["ts2", "ts1"]  # by name, case-folded
    upd = await db.update_term_set("ts1", "Chips2", [Term(term="SoC")])
    assert upd is not None and upd.name == "Chips2" and upd.terms == [Term(term="SoC")]
    assert await db.update_term_set("nope", "x", []) is None
    assert await db.delete_term_set("ts1") is True
    assert await db.get_term_set("ts1") is None
    assert await db.delete_term_set("ts1") is False


async def test_settings_kv(db: Database) -> None:
    assert await db.get_settings() == {}
    await db.set_setting("cloud.model", "a")
    await db.set_setting("cloud.model", "b")
    await db.set_setting("provider_default", "cloud")
    assert await db.get_settings() == {"cloud.model": "b", "provider_default": "cloud"}
    assert await db.delete_setting("cloud.model") is True
    assert await db.delete_setting("cloud.model") is False
    assert await db.get_settings() == {"provider_default": "cloud"}
