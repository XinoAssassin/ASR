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
        ("这个问题我觉得这个这个这个这个这个很好", "这个问题我觉得这个很好"),
        # numbers are data: never collapsed
        ("预算100000000元", "预算100000000元"),
        ("手机号13800000000", "手机号13800000000"),
        ("编号1212121212121212", "编号1212121212121212"),
        # runs of punctuation / whitespace are left alone
        ("。。。。。。。。。。", "。。。。。。。。。。"),
        ("等一下          好", "等一下          好"),
    ],
)
def test_collapse_repetitions(text: str, expected: str) -> None:
    out, edits = collapse_repetitions(text)
    assert out == expected
    assert bool(edits) == (text != expected)
    for offset, _, after in edits:
        assert out[offset : offset + len(after)] == after


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
    assert [c.offset for c in corrections] == [0, 0]
    off = CorrectionOptions(terms=False, repetition=False)
    assert correct_segments(raw, terms, off) == (raw, [])


def test_correct_segments_offsets_point_into_the_final_text() -> None:
    terms = [
        Term(term="Kubernetes", aliases=["库伯内特斯"]),
        Term(term="Townfall", aliases=["汤泡"]),
    ]
    raw = [
        seg(0, "Kubernetes 和库伯内特斯，哈哈哈哈哈哈哈哈哈哈，汤泡汤泡汤泡汤泡汤泡好"),
        seg(1, "汤泡和汤泡面，汤泡"),
    ]
    out, corrections = correct_segments(raw, terms, CorrectionOptions())
    texts = {s.id: s.text for s in out}
    assert texts["seg_00000"] == "Kubernetes 和Kubernetes，哈哈哈，Townfall好"
    for c in corrections:
        text = texts[c.segment_id]
        assert c.offset is not None and text[c.offset : c.offset + len(c.after)] == c.after
    first = [(c.source, c.before, c.after, c.offset) for c in corrections[:3]]
    assert first == [
        ("term", "库伯内特斯", "Kubernetes", 12),
        ("repetition", "哈哈哈哈哈哈哈哈哈哈", "哈哈哈", 23),
        # a term fix inside a collapsed repetition becomes one correction
        ("repetition", "汤泡汤泡汤泡汤泡汤泡", "Townfall", 27),
    ]
    # rebuilding the raw text from the corrections, right to left, gives the input back
    for s_raw, s_out in zip(raw, out, strict=True):
        text = s_out.text
        mine = [c for c in corrections if c.segment_id == s_raw.id]
        for c in sorted(mine, key=lambda c: -(c.offset or 0)):
            assert c.offset is not None
            text = text[: c.offset] + c.before + text[c.offset + len(c.after) :]
        assert text == s_raw.text


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
    (c,) = t["corrections"]
    assert {k: c[k] for k in ("segment_id", "source", "before", "after", "score", "status")} == {
        "segment_id": "seg_00000",
        "source": "term",
        "before": "汤泡",
        "after": "Townfall",
        "score": 1.0,
        "status": "applied",
    }
    assert c["id"]
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
    from asr import finalize as finalize_module

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("corrector exploded")

    monkeypatch.setattr(finalize_module, "correct_segments", boom)
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
