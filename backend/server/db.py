"""SQLite persistence (aiosqlite). Owned by ``backend-jobs`` after Phase 1.

Upload repository methods are implemented; job / transcript / term-set / settings repositories are
added by ``backend-jobs``. Chunked-upload sessions are not stored here: ``server.storage`` keeps
them on disk next to their parts.

JSON columns hold pydantic ``model_dump_json()`` output. Datetimes are ISO 8601 strings in UTC.
"""

from datetime import datetime
from pathlib import Path
from typing import Self

import aiosqlite

from asr.audio.types import ProbeInfo
from server.schemas import UploadOut

SCHEMA = """
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


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: Path) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

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

    # ------------------------------------------------------------------- jobs (backend-jobs)
    # insert_job / get_job / list_jobs / update_job / delete_job

    # ------------------------------------------------------------ transcripts (backend-jobs)
    # insert_transcript / get_transcript / update_transcript

    # -------------------------------------------------------------- term sets (backend-jobs)
    # list_term_sets / get_term_set / insert_term_set / update_term_set / delete_term_set

    # --------------------------------------------------------------- settings (backend-jobs)
    # get_settings / set_setting / delete_setting
