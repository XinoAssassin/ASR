"""Review regressions for the L3 guard (m7) and LLM client lifetime (n2); respx, no network."""

from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest
import respx

from asr.correct.llm import MAX_EDIT_RATIO, LlmCorrector, diff_corrections, unsanctioned_ratio
from asr.providers.base import Correction, Segment, Speaker, Term
from asr.summarize import Summarizer

ENDPOINT = "https://dashscope.aliyuncs.com"
CHAT = f"{ENDPOINT}/compatible-mode/v1/chat/completions"
KEY = "sk-test-key-0000000000"


def _fix(before: str, after: str) -> Correction:
    return Correction(segment_id="s", source="llm", before=before, after=after, status="suggested")


# ---------------------------------------------------------------------------------------- m7
def test_a_long_before_replaced_by_a_term_is_not_exempt() -> None:
    terms = [Term(term="Townfall")]
    clause = "我们决定把整个项目取消并且下周不再继续开会讨论这件事情了好吗"  # 30 chars
    old = clause + "，就这样吧。"
    assert unsanctioned_ratio(old, [_fix(clause, "Townfall")], terms) > MAX_EDIT_RATIO


def test_a_real_mishearing_of_a_term_stays_exempt() -> None:
    terms = [Term(term="Townfall"), Term(term="通义"), Term(term="Kubernetes")]
    assert unsanctioned_ratio("我在玩汤泡呢", [_fix("汤泡", "Townfall")], terms) == 0.0
    assert unsanctioned_ratio("用tong yi模型", [_fix("tong yi", "通义")], terms) == 0.0
    assert (
        unsanctioned_ratio("部署到酷伯内特斯上", [_fix("酷伯内特斯", "Kubernetes")], terms) == 0.0
    )


@respx.mock(assert_all_mocked=True)
def test_suggest_discards_a_clause_rewritten_into_a_term(respx_mock: respx.MockRouter) -> None:
    text = "我们决定把整个项目取消并且下周不再继续开会讨论这件事情了，就这样吧。"
    seg = Segment(id="s", speaker_id="A", start=0, end=1, text=text)
    found = diff_corrections("s", text, "Townfall，就这样吧。")
    assert len(found) == 1 and len(found[0].before) > 20
    content = json.dumps({"segments": [{"id": "s", "text": "Townfall，就这样吧。"}]})
    respx_mock.post(CHAT).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )
    with httpx.Client() as http:
        corrections, discarded = LlmCorrector(api_key=KEY, endpoint=ENDPOINT, http=http).suggest(
            [seg], [Term(term="Townfall")]
        )
    assert corrections == [] and discarded == 1


# ---------------------------------------------------------------------------------------- n2
class _Tracking(httpx.Client):
    instances: ClassVar[list[_Tracking]] = []

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        _Tracking.instances.append(self)


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch) -> list[_Tracking]:
    _Tracking.instances = []
    monkeypatch.setattr(httpx, "Client", _Tracking)
    return _Tracking.instances


@respx.mock(assert_all_mocked=True)
def test_corrector_without_a_client_closes_the_one_it_opens(
    respx_mock: respx.MockRouter, tracked: list[_Tracking]
) -> None:
    content = json.dumps({"segments": []})
    respx_mock.post(CHAT).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )
    corrector = LlmCorrector(api_key=KEY, endpoint=ENDPOINT)
    assert tracked == []  # nothing opened up front
    corrector.suggest([Segment(id="s", speaker_id="A", start=0, end=1, text="你好")])
    assert tracked and all(c.is_closed for c in tracked)


@respx.mock(assert_all_mocked=True)
def test_summarizer_without_a_client_closes_the_one_it_opens(
    respx_mock: respx.MockRouter, tracked: list[_Tracking]
) -> None:
    content = json.dumps(
        {"overview": "概述", "key_points": ["一"], "decisions": [], "action_items": []}
    )
    respx_mock.post(CHAT).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )
    summarizer = Summarizer(api_key=KEY, endpoint=ENDPOINT)
    assert tracked == []
    summarizer.summarize(
        [Segment(id="s", speaker_id="A", start=0, end=1, text="我们讨论了预算。")],
        [Speaker(id="A", name="甲", color="#1f77b4")],
    )
    assert tracked and all(c.is_closed for c in tracked)


def test_a_supplied_client_is_left_open() -> None:
    with httpx.Client() as http:
        LlmCorrector(api_key=KEY, endpoint=ENDPOINT, http=http)
        Summarizer(api_key=KEY, endpoint=ENDPOINT, http=http)
        assert not http.is_closed
