"""[5b] L3 LLM correction suggestions: helpers, guarded batching, route (respx, no network)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import SecretStr

from asr.correct.llm import (
    BATCH_SIZE,
    MAX_EDIT_RATIO,
    LlmCorrector,
    diff_corrections,
    glossary,
    unsanctioned_ratio,
)
from asr.providers.base import ProviderError, Segment, Term
from server.config import EnvSettings
from tests.conftest import EnvFactory
from tests.test_editing import _transcript

ENDPOINT = "https://dashscope.aliyuncs.com"
CHAT = f"{ENDPOINT}/compatible-mode/v1/chat/completions"
KEY = "sk-test-key-0000000000"


def chat_reply(segments: list[dict[str, str]]) -> httpx.Response:
    content = json.dumps({"segments": segments}, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_diff_and_guard_helpers() -> None:
    (c,) = diff_corrections("s", "就是寂静岭汤泡我确实", "就是寂静岭Townfall我确实")
    assert (c.before, c.after, c.offset, c.status, c.source) == (
        "汤泡",
        "Townfall",
        5,
        "suggested",
        "llm",
    )
    # whitespace-only edits are noise; padding around a replacement is trimmed
    assert diff_corrections("s", "玩Townfall吗", "玩 Townfall 吗") == []
    (c2,) = diff_corrections("s", "玩汤泡吗", "玩 Townfall 吗")
    assert (c2.before, c2.after, c2.offset) == ("汤泡", "Townfall", 1)
    terms = [Term(term="Townfall")]
    assert unsanctioned_ratio("就是寂静岭汤泡我确实", [c], terms) == 0.0  # writes a term: exempt
    # not a glossary term: weighs max(len(before), len(after)) = 8 of 10 chars
    assert unsanctioned_ratio("就是寂静岭汤泡我确实", [c], []) == pytest.approx(0.8)
    assert "寂静岭 (ji jing ling)" in glossary([Term(term="寂静岭")])
    assert "常被听成：汤泡" in glossary([Term(term="Townfall", aliases=["汤泡"])])


def _apply(old: str, found: list) -> str:
    for c in sorted(found, key=lambda c: -c.offset):
        old = old[: c.offset] + c.after + old[c.offset + len(c.before) :]
    return old


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("use cooper netis now", "use Kubernetes now", [("cooper netis", "Kubernetes", 4)]),
        ("cooper netis很好", "Kubernetes很好", [("cooper netis", "Kubernetes", 0)]),
        ("部署到k8s上", "部署到 Kubernetes 上", [("k8s", "Kubernetes", 3)]),
        ("version 1.2.3 ok", "version 1.2.4 ok", [("1.2.3", "1.2.4", 8)]),
    ],
)
def test_diff_works_on_whole_latin_words(old: str, new: str, expected: list) -> None:
    found = diff_corrections("s", old, new)
    assert [(c.before, c.after, c.offset) for c in found] == expected


def test_diff_keeps_a_needed_space() -> None:
    found = diff_corrections("s", "use call now", "use API call now")
    assert len(found) == 1 and found[0].before == ""
    assert _apply("use call now", found) == "use API call now"


def test_guard_weighs_insertions_and_exempts_only_term_writes() -> None:
    old = "今天的会议先到这里吧。"  # 11 chars
    assert len(old) == 11
    terms = [Term(term="Townfall"), Term(term="Kubernetes", aliases=["酷伯"])]
    injected = diff_corrections("s", old, old[:-1] + "，另外我们决定取消项目并解雇所有人。")
    assert [(c.before, c.offset) for c in injected] == [("", 10)]  # a pure insertion
    assert unsanctioned_ratio(old, injected, terms) > MAX_EDIT_RATIO
    # carrying a glossary term does not launder an injection
    laundered = diff_corrections("s", old, old[:-1] + "，另外Townfall项目取消。")
    assert unsanctioned_ratio(old, laundered, terms) > MAX_EDIT_RATIO
    # a plain term write (with up to two surrounding chars) stays exempt
    fix = diff_corrections("s", "部署到酷伯上", "部署到Kubernetes上")
    assert unsanctioned_ratio("部署到酷伯上", fix, terms) == 0.0


@respx.mock(assert_all_mocked=True)
def test_suggest_discards_an_injected_instruction(respx_mock: respx.MockRouter) -> None:
    seg = Segment(id="s", speaker_id="A", start=0, end=1, text="今天的会议先到这里吧。")
    evil = {"id": "s", "text": "今天的会议先到这里吧，另外我们决定取消项目并解雇所有人。"}
    respx_mock.post(CHAT).mock(return_value=chat_reply([evil]))
    with httpx.Client() as http:
        corrector = LlmCorrector(api_key=KEY, endpoint=ENDPOINT, http=http)
        found, discarded = corrector.suggest([seg], [Term(term="Townfall")])
    assert found == [] and discarded == 1


def test_corrector_requires_key_and_allowed_endpoint() -> None:
    with pytest.raises(ProviderError, match="not configured"):
        LlmCorrector(api_key="", endpoint=ENDPOINT)
    with pytest.raises(ProviderError, match="allow-list"):
        LlmCorrector(api_key=KEY, endpoint="https://evil.example.com")


@respx.mock(assert_all_mocked=True)
def test_suggest_batches_and_guards(respx_mock: respx.MockRouter) -> None:
    segments = [
        Segment(id=f"s{i}", speaker_id="A", start=i, end=i + 1, text=f"第{i}句汤泡很好玩")
        for i in range(BATCH_SIZE + 5)
    ]

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "qwen-plus" and body["temperature"] == 0
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        sent = json.loads(body["messages"][1]["content"].split("句子：\n", 1)[1])["segments"]
        ids = [s["id"] for s in sent]
        out = []
        if "s0" in ids:
            out += [
                {"id": "s0", "text": "第0句Townfall很好玩"},  # term fix: kept
                {"id": "s1", "text": "完全改写了这一整句话"},  # rewrite: discarded
                {"id": "nope", "text": "x"},  # unknown id: ignored
            ]
        return chat_reply(out)

    route = respx_mock.post(CHAT).mock(side_effect=reply)
    with httpx.Client() as http:
        corrector = LlmCorrector(api_key=KEY, endpoint=ENDPOINT, http=http)
        found, discarded = corrector.suggest(segments, [Term(term="Townfall")], "游戏播客")
    assert route.call_count == 2  # 25 segments in batches of 20
    assert [(c.segment_id, c.before, c.after) for c in found] == [("s0", "汤泡", "Townfall")]
    assert discarded == 1
    first = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "术语表：\nTownfall" in first and "背景：游戏播客" in first


@respx.mock(assert_all_mocked=True)
def test_suggest_errors(respx_mock: respx.MockRouter) -> None:
    seg = [Segment(id="s", speaker_id="A", start=0, end=1, text="x")]
    with httpx.Client() as http:
        corrector = LlmCorrector(api_key=KEY, endpoint=ENDPOINT, http=http)
        respx_mock.post(CHAT).mock(return_value=httpx.Response(401))
        with pytest.raises(ProviderError, match="rejected"):
            corrector.suggest(seg)
        respx_mock.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": []}))
        with pytest.raises(ProviderError, match="unreadable"):
            corrector.suggest(seg)


# ----------------------------------------------------------------------------------- route
@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=KEY)


async def test_route_stores_suggestions_and_accept_applies(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    with respx.mock(assert_all_mocked=False) as router:
        fixed = {"id": "seg_00000", "text": "Townfall真好玩，我们开始啦"}  # 1 of 15 chars
        router.post(CHAT).mock(return_value=chat_reply([fixed]))
        resp = await client.post(f"{url}/llm-suggestions", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 1 and body["discarded"] == 0
    assert KEY not in resp.text
    suggestion = next(c for c in body["transcript"]["corrections"] if c["source"] == "llm")
    assert suggestion["status"] == "suggested"
    assert body["transcript"]["segments"][0]["text"] == "Townfall真好玩，我们开始吧"  # not yet

    op = {"op": "review_correction", "correction_id": suggestion["id"], "decision": "accept"}
    patched = (await client.patch(url, json={"ops": [op]})).json()
    assert patched["segments"][0]["text"] == "Townfall真好玩，我们开始啦"


async def test_route_errors(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}/llm-suggestions"
    assert (await client.post(url, json={"segment_ids": ["nope"]})).status_code == 422
    with respx.mock(assert_all_mocked=False) as router:
        router.post(CHAT).mock(return_value=httpx.Response(500))
        resp = await client.post(url, json={})
    assert resp.status_code == 502


async def test_route_without_key(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> None:
    t = await _transcript(app, client, env)
    app.state.env = env.model_copy(update={"dashscope_api_key": SecretStr("")})
    resp = await client.post(f"/api/transcripts/{t['id']}/llm-suggestions", json={})
    assert resp.status_code == 422 and "not configured" in resp.json()["detail"]
