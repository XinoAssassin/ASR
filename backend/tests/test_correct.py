"""[5b] post-correction: repetition filter, L1 term correction, job integration, storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from asr.correct import TermCorrector, collapse_repetitions, correct_segments, hotword_lines
from asr.providers.base import CorrectionOptions, Segment, Speaker, Term, Transcript
from server.config import EnvSettings
from server.db import SCHEMA_VERSION, Database
from tests.conftest import EnvFactory
from tests.test_jobs import DASHSCOPE_KEY, FakeProvider, add_upload, install, wait_status


def seg(i: int, text: str) -> Segment:
    return Segment(id=f"seg_{i:05d}", speaker_id="SPEAKER_00", start=i, end=i + 1, text=text)


# --------------------------------------------------------------------------------- functions
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("好好好，对对对对", "好好好，对对对对"),  # normal speech stays
        ("哈哈哈哈哈哈哈哈哈哈", "哈哈哈"),
        ("Townfall，Townfall，Townfall，Townfall，Townfall，好", "Townfall，好"),
        ("我们我们我们我们我们我们一起", "我们一起"),
    ],
)
def test_collapse_repetitions(text: str, expected: str) -> None:
    out, edits = collapse_repetitions(text)
    assert out == expected
    assert bool(edits) == (text != expected)


def test_hotword_lines() -> None:
    terms = [
        Term(term="Townfall", aliases=["汤泡"], blacklist=["泡面"]),
        Term(term="寂静岭"),
    ]
    assert hotword_lines(terms) == "Townfall | 汤泡 ~~~ 泡面\n寂静岭"


def test_term_corrector_alias_threshold_and_blacklist() -> None:
    tc = TermCorrector(
        [Term(term="Townfall", aliases=["汤泡"]), Term(term="寂静岭")], threshold=0.8
    )
    text, matches = tc.correct("就是基金岭汤泡，我确实不是很想玩")
    assert text == "就是寂静岭Townfall，我确实不是很想玩"
    assert {(a, b) for a, b, _ in matches} == {("基金岭", "寂静岭"), ("汤泡", "Townfall")}
    assert tc.correct("今天天气不错，我们去吃饭吧")[1] == []
    strict = TermCorrector([Term(term="寂静岭")], threshold=0.9)
    assert strict.correct("就是基金岭")[0] == "就是基金岭"  # 0.83 < 0.9
    blocked = TermCorrector([Term(term="Townfall", aliases=["汤泡"], blacklist=["泡面"])])
    assert blocked.correct("吃泡面喝汤泡一下")[0] == "吃泡面喝汤泡一下"


def test_correct_segments_records_edits_and_keeps_input() -> None:
    raw = [seg(0, "Townfall，Townfall，Townfall，Townfall，Townfall，好"), seg(1, "汤泡真吓人")]
    terms = [Term(term="Townfall", aliases=["汤泡"])]
    out, corrections = correct_segments(raw, terms, CorrectionOptions())
    assert [s.text for s in out] == ["Townfall，好", "Townfall真吓人"]
    assert raw[1].text == "汤泡真吓人"  # inputs untouched
    assert [(c.segment_id, c.source, c.after) for c in corrections] == [
        ("seg_00000", "repetition", "Townfall，"),
        ("seg_00001", "term", "Townfall"),
    ]
    off = CorrectionOptions(terms=False, repetition=False)
    assert correct_segments(raw, terms, off) == (raw, [])


# ------------------------------------------------------------------------------- server path
@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


def _result(*texts: str) -> Transcript:
    return Transcript(
        provider="cloud",
        duration=10.0,
        speakers=[Speaker(id="SPEAKER_00", name="SPEAKER_00", color="#1f77b4")],
        segments=[seg(i, t) for i, t in enumerate(texts)],
    )


async def test_job_stores_raw_original_and_corrections(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    install(app, FakeProvider(result=_result("汤泡我确实不是很想玩", "今天天气不错")))
    upload = await add_upload(app, env)
    body = {"upload_id": upload.id, "extra_terms": [{"term": "Townfall", "aliases": ["汤泡"]}]}
    job = (await client.post("/api/jobs", json=body)).json()
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "succeeded", done
    t = (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()
    assert [s["text"] for s in t["segments"]] == ["Townfall我确实不是很想玩", "今天天气不错"]
    assert [s["text"] for s in t["original"]] == ["汤泡我确实不是很想玩", "今天天气不错"]
    assert t["corrections"] == [
        {
            "segment_id": "seg_00000",
            "source": "term",
            "before": "汤泡",
            "after": "Townfall",
            "score": 1.0,
        }
    ]
    reverted = (await client.post(f"/api/transcripts/{t['id']}/revert")).json()
    assert reverted["segments"][0]["text"] == "汤泡我确实不是很想玩"
    assert reverted["corrections"] == []


async def test_job_without_correction(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    install(app, FakeProvider(result=_result("汤泡我确实不是很想玩")))
    upload = await add_upload(app, env)
    body = {
        "upload_id": upload.id,
        "extra_terms": [{"term": "Townfall", "aliases": ["汤泡"]}],
        "options": {"correction": {"terms": False, "repetition": False}},
    }
    job = (await client.post("/api/jobs", json=body)).json()
    done = await wait_status(client, job["id"], {"succeeded"})
    t = (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()
    assert t["segments"][0]["text"] == "汤泡我确实不是很想玩" and t["corrections"] == []


async def test_correction_failure_keeps_raw_text(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import jobs as jobs_module

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("corrector exploded")

    monkeypatch.setattr(jobs_module, "correct_segments", boom)
    install(app, FakeProvider(result=_result("汤泡")))
    upload = await add_upload(app, env)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded", "failed"})
    assert done["status"] == "succeeded"
    t = (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()
    assert t["segments"][0]["text"] == "汤泡"
    assert any("correction failed" in w for w in t["warnings"])


async def test_schema_v2_database_gets_corrections_column(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = await Database.open(path)
    await db.close()
    # Simulate a v2 database: drop the column by rebuilding the table without it.
    con = sqlite3.connect(path)
    con.executescript(
        """
        ALTER TABLE transcripts RENAME TO t_old;
        CREATE TABLE transcripts AS SELECT id, job_id, provider, duration, language,
            speakers_json, segments_json, original_json, warnings_json, created_at, updated_at
            FROM t_old;
        DROP TABLE t_old;
        DELETE FROM schema_version;
        INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-10-29T00:00:00+00:00');
        """
    )
    con.commit()
    con.close()
    db = await Database.open(path)
    try:
        assert await db.schema_version() == SCHEMA_VERSION
        async with db.conn.execute("PRAGMA table_info(transcripts)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        assert "corrections_json" in columns
    finally:
        await db.close()
