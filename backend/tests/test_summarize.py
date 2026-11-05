"""M11 LLM summary: text building, chunked map/reduce, parsing guards, citations (refs), DB v4,
routes (respx)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import SecretStr

from asr.providers.base import ProviderError, Segment, Speaker, Term
from asr.summarize import (
    LINE_CHARS,
    MERGE_PROMPT,
    SYSTEM_PROMPT,
    Summarizer,
    TranscriptSummary,
    chunk_lines,
    parse_summary,
    summary_markdown,
    transcript_lines,
)
from server.config import EnvSettings
from server.db import SCHEMA_VERSION, Database
from tests.conftest import EnvFactory
from tests.test_editing import _transcript

ENDPOINT = "https://dashscope.aliyuncs.com"
CHAT = f"{ENDPOINT}/compatible-mode/v1/chat/completions"
KEY = "sk-test-key-0000000000"

SPEAKERS = [
    Speaker(id="SPEAKER_00", name="张三", color="#000000"),
    Speaker(id="SPEAKER_01", name="SPEAKER_01", color="#111111"),
]

ANSWER = {
    "overview": "讨论了 Townfall 的发布计划。",
    "key_points": ["发布推迟一周", "", 42, {"bad": 1}],
    "decisions": [{"text": "下周三发布", "refs": ["#2", "#99"]}],
    "action_items": [
        {"task": "准备发布说明", "owner": "张三", "due": "周二", "refs": ["#1"]},
        {"task": "检查服务器", "owner": "null", "due": None},
        {"task": "", "owner": "x"},
        "写周报",
    ],
}


def chat_reply(answer: dict[str, object]) -> httpx.Response:
    content = json.dumps(answer, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def seg(sid: str, start: float, text: str, speaker: str = "SPEAKER_00") -> Segment:
    return Segment(id=sid, speaker_id=speaker, start=start, end=start + 1, text=text)


def test_transcript_lines_join_turns_and_use_names() -> None:
    segments = [
        seg("b", 3725.0, "好的。", "SPEAKER_01"),
        seg("a2", 2.0, "Docker"),
        seg("a1", 1.0, "我们用"),
        seg("a3", 2.5, "Compose 部署。"),
        seg("c", 3800.0, "  "),
    ]
    lines = transcript_lines(segments, SPEAKERS)
    assert [line.text for line in lines] == [
        "[#1 00:01] 张三: 我们用Docker Compose 部署。",
        "[#2 1:02:05] SPEAKER_01: 好的。",
    ]
    assert [(line.ref, line.segment_id) for line in lines] == [("1", "a1"), ("2", "b")]


def test_transcript_lines_cut_long_turns_at_segment_boundaries() -> None:
    long = "很" * (LINE_CHARS - 3)
    segments = [
        seg("a", 0, long),
        seg("b", 5, "第二句\n换行"),  # still under the limit: joined
        seg("c", 9, "第三句"),  # the line is over the limit now: a new line
        seg("d", 12, "回答", "SPEAKER_01"),
    ]
    lines = transcript_lines(segments, SPEAKERS)
    assert [(line.ref, line.segment_id) for line in lines] == [
        ("1", "a"),
        ("2", "c"),
        ("3", "d"),
    ]
    assert lines[0].text == f"[#1 00:00] 张三: {long}第二句 换行"
    assert lines[1].text == "[#2 00:09] 张三: 第三句"


def test_chunk_lines_respects_limit() -> None:
    lines = ["a" * 40, "b" * 40, "c" * 40, "d" * 130]
    chunks = chunk_lines(lines, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == "".join(lines)
    assert chunks[0] == "a" * 40 + "\n" + "b" * 40


def test_parse_summary_clamps_and_drops_bad_values() -> None:
    s = parse_summary(ANSWER, model="qwen-plus")
    assert [(p.text, p.refs) for p in s.key_points] == [("发布推迟一周", []), ("42", [])]
    assert [(a.task, a.owner, a.due, a.refs) for a in s.action_items] == [
        ("准备发布说明", "张三", "周二", []),  # no reference map: refs dropped
        ("检查服务器", None, None, []),
        ("写周报", None, None, []),
    ]
    assert s.model == "qwen-plus" and s.chunks == 1
    with pytest.raises(ProviderError, match="empty"):
        parse_summary({"overview": " ", "key_points": "not a list"}, model="m")
    md = summary_markdown(s)
    assert "## 待办" in md and "- 准备发布说明（张三，周二）\n" in md


def test_parse_summary_validates_and_maps_refs() -> None:
    known = {str(n): f"seg_{n:05d}" for n in range(1, 21)}
    answer = {
        "overview": "概述",
        "key_points": [
            {"text": "一", "refs": ["#12", "#3", "#12", "#99", "#0", None, {"x": 1}, True]},
            {"text": "二", "refs": [5, "7", " #8 ", "#9 03:41", "[#10 00:01]", "00:41"]},
            {"text": "三", "refs": "#4, #2"},
            {"text": "四", "refs": "#6"},
            {"content": "五", "refs": "bad"},
            {"refs": ["#1"]},  # no text: dropped
        ],
        "decisions": [{"text": "定了", "refs": ["#20", "#21"]}],
        "action_items": [{"task": "做", "refs": ["#1", "#2", "#3", "#4"]}, {"task": "无"}],
    }
    s = parse_summary(answer, model="m", refs=known)
    assert [(p.text, p.refs) for p in s.key_points] == [
        ("一", ["seg_00003", "seg_00012"]),  # unknown dropped, deduplicated, transcript order
        ("二", ["seg_00005", "seg_00007", "seg_00008"]),  # at most three
        ("三", ["seg_00002", "seg_00004"]),
        ("四", ["seg_00006"]),
        ("五", []),
    ]
    assert [(d.text, d.refs) for d in s.decisions] == [("定了", ["seg_00020"])]
    assert [a.refs for a in s.action_items] == [["seg_00001", "seg_00002", "seg_00003"], []]

    segments = [seg("seg_00003", 221.7, "x"), seg("seg_00001", 3725, "y")]
    md = summary_markdown(s, segments=segments)
    assert "- 一（00:03:41）\n" in md  # first cited segment that exists
    assert "- 二\n" in md  # none of its segments exists any more
    assert "- 做（01:02:05）\n" in md


def test_summaries_without_refs_still_load() -> None:
    old = {
        "overview": "旧摘要",
        "key_points": ["要点一"],
        "decisions": ["决定一"],
        "action_items": [{"task": "任务", "owner": None, "due": None}],
        "model": "qwen-plus",
        "chunks": 1,
        "created_at": "2026-10-29T07:00:00Z",
    }
    s = TranscriptSummary.model_validate_json(json.dumps(old, ensure_ascii=False))
    assert s.model_dump(mode="json")["key_points"] == [{"text": "要点一", "refs": []}]
    assert s.decisions[0].text == "决定一" and s.action_items[0].refs == []
    assert "- 决定一\n" in summary_markdown(s, segments=[seg("a", 1, "x")])


def test_summarizer_requires_key_and_allowed_endpoint() -> None:
    with pytest.raises(ProviderError, match="not configured"):
        Summarizer(api_key="", endpoint=ENDPOINT)
    with pytest.raises(ProviderError, match="allow-list"):
        Summarizer(api_key=KEY, endpoint="https://evil.example.com")


@respx.mock(assert_all_mocked=True)
def test_single_call_for_short_transcript(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(CHAT).mock(return_value=chat_reply(ANSWER))
    segments = [seg("a", 0, "我们讨论汤泡的发布"), seg("b", 2, "好", "SPEAKER_01")]
    with httpx.Client() as http:
        summary = Summarizer(api_key=KEY, endpoint=ENDPOINT, http=http).summarize(
            segments, SPEAKERS, [Term(term="Townfall", aliases=["汤泡"])], "产品例会"
        )
    assert route.call_count == 1 and summary.chunks == 1
    assert [(d.text, d.refs) for d in summary.decisions] == [("下周三发布", ["b"])]  # #99 dropped
    assert summary.action_items[0].refs == ["a"]
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen-plus" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"] == SYSTEM_PROMPT
    user = body["messages"][1]["content"]
    assert "说话人：SPEAKER_01、张三" in user and "术语表：\nTownfall" in user
    assert "背景：产品例会" in user and "[#1 00:00] 张三: 我们讨论汤泡的发布" in user
    assert "[#2 00:02] SPEAKER_01: 好" in user
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {KEY}"


@respx.mock(assert_all_mocked=True)
def test_long_transcript_map_reduce(respx_mock: respx.MockRouter) -> None:
    merge_parts: list[list[dict[str, object]]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["messages"][0]["content"] == MERGE_PROMPT:
            parts = json.loads(body["messages"][1]["content"].split("部分摘要：\n", 1)[1])
            merge_parts.append(parts)
            assert len(parts) == 2  # the empty third chunk is skipped
            assert set(parts[0]) == {"overview", "key_points", "decisions", "action_items"}
            return chat_reply(
                {
                    "overview": "合并",
                    "key_points": [{"text": "全部", "refs": ["#3", "#1", "#2"]}],
                    "decisions": [{"text": "定", "refs": ["#4"]}],  # cited by no partial
                    "action_items": [{"task": "做", "refs": ["#3"]}],
                }
            )
        user = body["messages"][1]["content"]
        if "第 3/3 部分" in user:
            return chat_reply({"overview": "", "key_points": []})
        if "第 1/3 部分" in user:  # lines #1 and #2; #3 belongs to the next chunk
            assert "[#1 00:00]" in user and "[#2 00:10]" in user and "[#3 " not in user
            return chat_reply(
                {"overview": "一", "key_points": [{"text": "甲", "refs": ["#2", "#1", "#3"]}]}
            )
        assert "[#3 00:20]" in user and "[#4 00:30]" in user
        return chat_reply(
            {
                "overview": "二",
                "key_points": [{"text": "乙", "refs": ["#4"]}],
                "action_items": [{"task": "做", "refs": ["#3", "#9"]}],
            }
        )

    route = respx_mock.post(CHAT).mock(side_effect=reply)
    segments = [seg(f"s{i}", i * 10, "这是一句很长的话" * 5, f"SPEAKER_0{i % 2}") for i in range(6)]
    seen: list[tuple[int, int]] = []
    with httpx.Client() as http:
        summarizer = Summarizer(api_key=KEY, endpoint=ENDPOINT, http=http, chunk_chars=150)
        summary = summarizer.summarize(
            segments, SPEAKERS, progress=lambda a, b: seen.append((a, b))
        )
    assert route.call_count == 4  # 3 chunks + merge
    assert summary.overview == "合并" and summary.chunks == 3
    # the merge call sees the chunk results with their (validated) "#n" references
    assert merge_parts[0][0]["key_points"] == [{"text": "甲", "refs": ["#1", "#2"]}]
    assert merge_parts[0][1]["key_points"] == [{"text": "乙", "refs": ["#4"]}]
    assert merge_parts[0][1]["action_items"][0]["refs"] == ["#3"]
    # ... and its answer is mapped back to segment ids; #4 was cited by a partial (kept)
    assert summary.key_points[0].refs == ["s0", "s1", "s2"]
    assert summary.decisions[0].refs == ["s3"]
    assert summary.action_items[0].refs == ["s2"]
    assert seen[-1] == (4, 4)


@respx.mock(assert_all_mocked=True)
def test_errors(respx_mock: respx.MockRouter) -> None:
    segments = [seg("a", 0, "你好")]
    with httpx.Client() as http:
        s = Summarizer(api_key=KEY, endpoint=ENDPOINT, http=http)
        with pytest.raises(ProviderError, match="no text"):
            s.summarize([seg("a", 0, " ")], SPEAKERS)
        respx_mock.post(CHAT).mock(return_value=httpx.Response(401))
        with pytest.raises(ProviderError, match="rejected"):
            s.summarize(segments, SPEAKERS)
        respx_mock.post(CHAT).mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})
        )
        with pytest.raises(ProviderError, match="unreadable"):
            s.summarize(segments, SPEAKERS)


# ------------------------------------------------------------------------------------ db
async def test_v3_database_migrates_to_v4(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = await Database.open(path)
    await db.close()
    with sqlite3.connect(path) as conn:  # simulate a v3 file: no summary column
        conn.execute("ALTER TABLE transcripts DROP COLUMN summary_json")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version VALUES (3, '2026-10-29T00:00:00+00:00')")
    db = await Database.open(path)
    try:
        assert await db.schema_version() == SCHEMA_VERSION == 4
        async with db.conn.execute("PRAGMA table_info(transcripts)") as cur:
            assert "summary_json" in [row[1] for row in await cur.fetchall()]
    finally:
        await db.close()


# ---------------------------------------------------------------------------------- routes
@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=KEY)


async def test_route_creates_stores_and_deletes_summary(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    assert t["summary"] is None
    url = f"/api/transcripts/{t['id']}"
    assert (await client.get(f"{url}/summary")).status_code == 404
    with respx.mock(assert_all_mocked=False) as router:
        route = router.post(CHAT).mock(return_value=chat_reply(ANSWER))
        resp = await client.post(f"{url}/summary")
    assert resp.status_code == 200, resp.text
    assert KEY not in resp.text
    user = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "Townfall真好玩" in user  # current (corrected) segments, glossary from the job
    summary = resp.json()
    assert summary["decisions"] == [{"text": "下周三发布", "refs": ["seg_00001"]}]
    assert summary["action_items"][0]["refs"] == ["seg_00000"]
    assert summary["model"] == "qwen-plus"

    stored = (await client.get(url)).json()
    assert stored["summary"] == summary
    assert stored["updated_at"] == t["updated_at"]  # storing a summary is not a text edit
    assert (await client.get(f"{url}/summary")).json() == summary
    # edits and revert keep the summary
    op = {"op": "edit_text", "segment_id": "seg_00000", "text": "改过了"}
    assert (await client.patch(url, json={"ops": [op]})).json()["summary"] == summary
    assert (await client.post(f"{url}/revert")).json()["summary"] == summary

    assert (await client.delete(f"{url}/summary")).status_code == 204
    assert (await client.get(url)).json()["summary"] is None
    assert (await client.delete("/api/transcripts/nope/summary")).status_code == 404


async def test_route_old_summary_loads_and_exports(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    old = {  # as stored before citations existed: plain strings, no refs
        "overview": "旧摘要",
        "key_points": ["要点一"],
        "decisions": ["决定一"],
        "action_items": [{"task": "任务", "owner": "张三", "due": None}],
        "model": "qwen-plus",
        "chunks": 1,
        "created_at": "2099-01-01T00:00:00Z",
    }
    db: Database = app.state.db
    await db.conn.execute(
        "UPDATE transcripts SET summary_json = ? WHERE id = ?",
        (json.dumps(old, ensure_ascii=False), t["id"]),
    )
    await db.conn.commit()
    url = f"/api/transcripts/{t['id']}"
    summary = (await client.get(f"{url}/summary")).json()
    assert summary["key_points"] == [{"text": "要点一", "refs": []}]
    assert summary["action_items"][0] == {"task": "任务", "owner": "张三", "due": None, "refs": []}
    md = (await client.get(f"{url}/export", params={"format": "md"})).text
    assert "- 要点一\n" in md and "- [ ] 任务（负责人：张三）\n" in md

    with respx.mock(assert_all_mocked=False) as router:
        router.post(CHAT).mock(return_value=chat_reply(ANSWER))
        assert (await client.post(f"{url}/summary")).status_code == 200
    md = (await client.get(f"{url}/export", params={"format": "md"})).text
    assert "- 下周三发布（00:00:02）\n" in md  # seg_00001 starts at 2.5 s
    assert "- [ ] 准备发布说明（负责人：张三 · 期限：周二）（00:00:00）\n" in md
    en = (await client.get(f"{url}/export", params={"format": "md", "locale": "en"})).text
    assert "- 下周三发布 (00:00:02)\n" in en


async def test_route_errors(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}/summary"
    assert (await client.post("/api/transcripts/nope/summary")).status_code == 404
    with respx.mock(assert_all_mocked=False) as router:
        router.post(CHAT).mock(return_value=httpx.Response(500))
        assert (await client.post(url)).status_code == 502
    app.state.env = env.model_copy(update={"dashscope_api_key": SecretStr("")})
    resp = await client.post(url)
    assert resp.status_code == 422 and "not configured" in resp.json()["detail"]
    assert (await client.get(f"/api/transcripts/{t['id']}")).json()["summary"] is None


# ------------------------------------------------------------------------------------- cli
@respx.mock(assert_all_mocked=True)
def test_cli_summarize(
    respx_mock: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from asr import cli as core_cli
    from asr.providers.base import Transcript

    transcript = Transcript(provider="local", speakers=SPEAKERS, segments=[seg("a", 0, "你好")])
    src = tmp_path / "t.json"
    src.write_text(transcript.model_dump_json(), encoding="utf-8")
    runner = CliRunner()
    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_ENDPOINT", "ASR_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert runner.invoke(core_cli.app, ["summarize", str(src)]).exit_code == 2  # no key

    monkeypatch.setenv("DASHSCOPE_API_KEY", KEY)
    route = respx_mock.post(CHAT).mock(return_value=chat_reply(ANSWER))
    out = tmp_path / "s.json"
    result = runner.invoke(
        core_cli.app, ["summarize", str(src), "-f", "json", "-o", str(out), "-t", "Townfall"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8"))["decisions"] == [
        {"text": "下周三发布", "refs": []}  # #2 is not a line of this one-line transcript
    ]
    assert (
        "术语表：\nTownfall" in json.loads(route.calls[0].request.content)["messages"][1]["content"]
    )
    md = runner.invoke(core_cli.app, ["summarize", str(src)])
    assert md.exit_code == 0 and "# 摘要" in md.stdout
    assert "- 准备发布说明（张三，周二）（00:00:00）\n" in md.stdout  # cited item: its time
    assert "- 下周三发布\n" in md.stdout
