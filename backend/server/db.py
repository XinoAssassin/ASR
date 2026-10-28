"""SQLite persistence (aiosqlite). Owned by ``backend-jobs``.

Repositories for uploads, jobs, transcripts, term sets and the key/value settings table.
Chunked-upload sessions are not stored here: ``server.storage`` keeps them on disk next to their
parts.

JSON columns hold pydantic ``model_dump_json()`` output. Datetimes are ISO 8601 strings in UTC.
One connection serves the process; aiosqlite serialises statements on its own thread. Status
transitions that race (worker claim vs. cancel) use conditional ``UPDATE ... WHERE status IN``
so they are atomic in SQLite.
"""

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import aiosqlite
from pydantic import TypeAdapter

from asr.audio.types import ProbeInfo
from asr.providers.base import JobOptions, Segment, Speaker, Term, Transcript
from server.schemas import JobOut, JobStatus, TermSetOut, TranscriptOut, UploadOut

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SEGMENTS = TypeAdapter(list[Segment])
_SPEAKERS = TypeAdapter(list[Speaker])
_TERMS = TypeAdapter(list[Term])

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size          INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    probe_json    TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    upload_id     TEXT REFERENCES uploads(id) ON DELETE SET NULL,
    file_url      TEXT,
    provider      TEXT NOT NULL,
    options_json  TEXT NOT NULL,
    status        TEXT NOT NULL,
    stage         TEXT NOT NULL,
    progress      REAL NOT NULL DEFAULT 0,
    message       TEXT,
    error         TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    cloud_task_id TEXT,
    transcript_id TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS jobs_upload ON jobs(upload_id);

CREATE TABLE IF NOT EXISTS transcripts (
    id             TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    duration       REAL,
    language       TEXT,
    speakers_json  TEXT NOT NULL,
    segments_json  TEXT NOT NULL,
    original_json  TEXT NOT NULL,
    warnings_json  TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcripts_job ON transcripts(job_id);

CREATE TABLE IF NOT EXISTS term_sets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    terms_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _ts_or_none(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def utcnow() -> datetime:
    return datetime.now(UTC)


# Columns ``update_job`` may write, with their encoders.
_JOB_COLUMNS: dict[str, tuple[str, Any]] = {
    "status": ("status", None),
    "stage": ("stage", None),
    "progress": ("progress", None),
    "message": ("message", None),
    "error": ("error", None),
    "warnings": ("warnings_json", lambda v: json.dumps(list(v), ensure_ascii=False)),
    "cloud_task_id": ("cloud_task_id", None),
    "transcript_id": ("transcript_id", None),
    "options": ("options_json", lambda v: v.model_dump_json()),
    "started_at": ("started_at", _ts_or_none),
    "finished_at": ("finished_at", _ts_or_none),
}


def _job_from_row(row: aiosqlite.Row) -> JobOut:
    return JobOut(
        id=row["id"],
        upload_id=row["upload_id"],
        file_url=row["file_url"],
        provider=row["provider"],
        options=JobOptions.model_validate_json(row["options_json"]),
        status=row["status"],
        stage=row["stage"],
        progress=row["progress"],
        message=row["message"],
        error=row["error"],
        warnings=json.loads(row["warnings_json"]),
        cloud_task_id=row["cloud_task_id"],
        transcript_id=row["transcript_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
    )


def _transcript_from_row(row: aiosqlite.Row) -> TranscriptOut:
    return TranscriptOut(
        id=row["id"],
        job_id=row["job_id"],
        provider=row["provider"],
        duration=row["duration"],
        language=row["language"],
        speakers=_SPEAKERS.validate_json(row["speakers_json"]),
        segments=_SEGMENTS.validate_json(row["segments_json"]),
        original=_SEGMENTS.validate_json(row["original_json"]),
        warnings=json.loads(row["warnings_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _term_set_from_row(row: aiosqlite.Row) -> TermSetOut:
    return TermSetOut(
        id=row["id"],
        name=row["name"],
        terms=_TERMS.validate_json(row["terms_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _dump_segments(segments: Sequence[Segment]) -> str:
    return _SEGMENTS.dump_json(list(segments)).decode("utf-8")


def _dump_speakers(speakers: Sequence[Speaker]) -> str:
    return _SPEAKERS.dump_json(list(speakers)).decode("utf-8")


def _dump_terms(terms: Sequence[Term]) -> str:
    return _TERMS.dump_json(list(terms)).decode("utf-8")


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: Path) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await _enable_wal(conn)
        await conn.executescript(SCHEMA)
        async with conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        if row is None or row[0] is None:
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _ts(utcnow())),
            )
        await conn.commit()
        return cls(conn)

    async def schema_version(self) -> int:
        async with self.conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def journal_mode(self) -> str:
        async with self.conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else ""

    async def close(self) -> None:
        await self.conn.close()

    # ---------------------------------------------------------------------------- uploads
    async def insert_upload(self, upload: UploadOut) -> None:
        await self.conn.execute(
            "INSERT INTO uploads (id, path, original_name, size, created_at, probe_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                upload.id,
                upload.path,
                upload.original_name,
                upload.size,
                _ts(upload.created_at),
                upload.probe.model_dump_json() if upload.probe else None,
            ),
        )
        await self.conn.commit()

    async def get_upload(self, upload_id: str) -> UploadOut | None:
        async with self.conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return UploadOut(
            id=row["id"],
            path=row["path"],
            original_name=row["original_name"],
            size=row["size"],
            created_at=datetime.fromisoformat(row["created_at"]),
            probe=ProbeInfo.model_validate_json(row["probe_json"]) if row["probe_json"] else None,
        )

    async def delete_upload(self, upload_id: str) -> bool:
        """Delete the row; returns False if it did not exist. Files are removed by the caller."""
        cur = await self.conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------------------------- jobs
    async def insert_job(self, job: JobOut) -> None:
        await self.conn.execute(
            "INSERT INTO jobs (id, upload_id, file_url, provider, options_json, status, stage,"
            " progress, message, error, warnings_json, cloud_task_id, transcript_id, created_at,"
            " started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.upload_id,
                job.file_url,
                job.provider,
                job.options.model_dump_json(),
                job.status,
                job.stage,
                job.progress,
                job.message,
                job.error,
                json.dumps(job.warnings, ensure_ascii=False),
                job.cloud_task_id,
                job.transcript_id,
                _ts(job.created_at),
                _ts_or_none(job.started_at),
                _ts_or_none(job.finished_at),
            ),
        )
        await self.conn.commit()

    async def get_job(self, job_id: str) -> JobOut | None:
        async with self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return None if row is None else _job_from_row(row)

    async def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[JobOut], int]:
        """Newest first; returns ``(page, total matching)``."""
        where, params = ("WHERE status = ?", (status,)) if status else ("", ())
        async with self.conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params) as cur:  # noqa: S608
            total_row = await cur.fetchone()
        async with self.conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",  # noqa: S608
            (*params, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        total = int(total_row[0]) if total_row else 0
        return [_job_from_row(r) for r in rows], total

    async def list_jobs_by_status(self, *statuses: JobStatus) -> list[JobOut]:
        """Oldest first (queue order); used for startup recovery."""
        marks = ", ".join("?" for _ in statuses)
        async with self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at, id",  # noqa: S608
            statuses,
        ) as cur:
            rows = await cur.fetchall()
        return [_job_from_row(r) for r in rows]

    def _job_assignments(self, fields: dict[str, Any]) -> tuple[str, list[Any]]:
        sets: list[str] = []
        params: list[Any] = []
        for name, value in fields.items():
            if name not in _JOB_COLUMNS:
                raise ValueError(f"unknown job field {name!r}")
            column, encode = _JOB_COLUMNS[name]
            sets.append(f"{column} = ?")
            params.append(encode(value) if encode and value is not None else value)
        return ", ".join(sets), params

    async def update_job(self, job_id: str, **fields: Any) -> JobOut | None:
        """Write ``fields`` and return the updated job (None if it does not exist)."""
        if fields:
            sets, params = self._job_assignments(fields)
            await self.conn.execute(
                f"UPDATE jobs SET {sets} WHERE id = ?",  # noqa: S608 - column names are whitelisted
                (*params, job_id),
            )
            await self.conn.commit()
        return await self.get_job(job_id)

    async def transition_job(
        self, job_id: str, from_statuses: Iterable[JobStatus], **fields: Any
    ) -> JobOut | None:
        """Atomically apply ``fields`` only if the job's status is in ``from_statuses``.

        Returns the updated job, or None when the job is missing or in another status.
        """
        allowed = tuple(from_statuses)
        sets, params = self._job_assignments(fields)
        marks = ", ".join("?" for _ in allowed)
        cur = await self.conn.execute(
            f"UPDATE jobs SET {sets} WHERE id = ? AND status IN ({marks})",  # noqa: S608
            (*params, job_id, *allowed),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_job(job_id)

    async def delete_job(self, job_id: str) -> bool:
        """Delete the job and (FK cascade) its transcript."""
        cur = await self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def count_jobs_for_upload(
        self, upload_id: str, *, exclude_job_id: str | None = None
    ) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE upload_id = ?"
        params: tuple[str, ...] = (upload_id,)
        if exclude_job_id is not None:
            sql += " AND id != ?"
            params = (upload_id, exclude_job_id)
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -------------------------------------------------------------------------- transcripts
    async def insert_transcript(
        self, transcript_id: str, job_id: str, transcript: Transcript
    ) -> TranscriptOut:
        """Store ``transcript``; ``segments`` and the immutable ``original`` start equal."""
        now = _ts(utcnow())
        segments = _dump_segments(transcript.segments)
        await self.conn.execute(
            "INSERT INTO transcripts (id, job_id, provider, duration, language, speakers_json,"
            " segments_json, original_json, warnings_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transcript_id,
                job_id,
                transcript.provider,
                transcript.duration,
                transcript.language,
                _dump_speakers(transcript.speakers),
                segments,
                segments,
                json.dumps(transcript.warnings, ensure_ascii=False),
                now,
                now,
            ),
        )
        await self.conn.commit()
        result = await self.get_transcript(transcript_id)
        assert result is not None  # noqa: S101 - just inserted
        return result

    async def get_transcript(self, transcript_id: str) -> TranscriptOut | None:
        async with self.conn.execute(
            "SELECT * FROM transcripts WHERE id = ?", (transcript_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _transcript_from_row(row)

    async def update_transcript(
        self,
        transcript_id: str,
        *,
        segments: Sequence[Segment] | None = None,
        speakers: Sequence[Speaker] | None = None,
    ) -> TranscriptOut | None:
        """Update the editable columns. ``original`` has no setter (rule 8)."""
        sets = ["updated_at = ?"]
        params: list[Any] = [_ts(utcnow())]
        if segments is not None:
            sets.append("segments_json = ?")
            params.append(_dump_segments(segments))
        if speakers is not None:
            sets.append("speakers_json = ?")
            params.append(_dump_speakers(speakers))
        cur = await self.conn.execute(
            f"UPDATE transcripts SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
            (*params, transcript_id),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_transcript(transcript_id)

    async def revert_transcript(self, transcript_id: str) -> TranscriptOut | None:
        """``segments <- original``."""
        cur = await self.conn.execute(
            "UPDATE transcripts SET segments_json = original_json, updated_at = ? WHERE id = ?",
            (_ts(utcnow()), transcript_id),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_transcript(transcript_id)

    # ---------------------------------------------------------------------------- term sets
    async def list_term_sets(self) -> list[TermSetOut]:
        async with self.conn.execute(
            "SELECT * FROM term_sets ORDER BY name COLLATE NOCASE, created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [_term_set_from_row(r) for r in rows]

    async def get_term_set(self, term_set_id: str) -> TermSetOut | None:
        async with self.conn.execute("SELECT * FROM term_sets WHERE id = ?", (term_set_id,)) as cur:
            row = await cur.fetchone()
        return None if row is None else _term_set_from_row(row)

    async def insert_term_set(
        self, term_set_id: str, name: str, terms: Sequence[Term]
    ) -> TermSetOut:
        now = _ts(utcnow())
        await self.conn.execute(
            "INSERT INTO term_sets (id, name, terms_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (term_set_id, name, _dump_terms(terms), now, now),
        )
        await self.conn.commit()
        result = await self.get_term_set(term_set_id)
        assert result is not None  # noqa: S101 - just inserted
        return result

    async def update_term_set(
        self, term_set_id: str, name: str, terms: Sequence[Term]
    ) -> TermSetOut | None:
        cur = await self.conn.execute(
            "UPDATE term_sets SET name = ?, terms_json = ?, updated_at = ? WHERE id = ?",
            (name, _dump_terms(terms), _ts(utcnow()), term_set_id),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_term_set(term_set_id)

    async def delete_term_set(self, term_set_id: str) -> bool:
        cur = await self.conn.execute("DELETE FROM term_sets WHERE id = ?", (term_set_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------------------- settings
    async def get_settings(self) -> dict[str, str]:
        async with self.conn.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def delete_setting(self, key: str) -> bool:
        cur = await self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self.conn.commit()
        return cur.rowcount > 0


async def _enable_wal(conn: aiosqlite.Connection) -> None:
    """Use WAL when the filesystem supports it (drvfs may not); otherwise keep the default."""
    try:
        async with conn.execute("PRAGMA journal_mode = WAL") as cur:
            row = await cur.fetchone()
        mode = str(row[0]).lower() if row else ""
        if mode != "wal":
            log.info("SQLite WAL unavailable, journal_mode=%s", mode)
    except sqlite3.Error:
        log.info("SQLite WAL unavailable; using the default journal mode")
