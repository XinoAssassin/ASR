"""Unit tests for the cloud provider helpers: allow-list, redaction, body, normalisation."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from asr.providers import LOCAL_UNAVAILABLE, get_provider
from asr.providers.base import JobOptions, ProviderError, Term
from asr.providers.cloud import (
    ALLOWED_ENDPOINTS,
    DashScopeFiletransProvider,
    build_request_body,
    is_allowed_endpoint,
    test_connection,
)
from asr.providers.cloud.normalize import to_transcript
from asr.providers.cloud.security import is_aliyun_https_url, redact
from asr.providers.cloud.transfer import safe_object_name
from tests.fixtures.dashscope.support import API_KEY, ENDPOINT, MODEL, load


# ------------------------------------------------------------------------------ allow-list
@pytest.mark.parametrize(
    "url",
    [
        *ALLOWED_ENDPOINTS,
        "https://dashscope.aliyuncs.com/",
        "https://ws-1a2b3c.cn-beijing.maas.aliyuncs.com",
        "https://llm-abc123.cn-beijing.maas.aliyuncs.com",
    ],
)
def test_allowed_endpoints(url):
    assert is_allowed_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://dashscope.aliyuncs.com",
        "HTTPS://dashscope.aliyuncs.com",
        "https://DashScope.aliyuncs.com",
        "https://dashscope.aliyuncs.com:443",
        "https://dashscope.aliyuncs.com:8443",
        "https://user:pw@dashscope.aliyuncs.com",
        "https://dashscope.aliyuncs.com/api/v1",
        "https://dashscope.aliyuncs.com?x=1",
        "https://dashscope.aliyuncs.com#frag",
        "https://dashscope.aliyuncs.com.evil.com",
        "https://evil.com/dashscope.aliyuncs.com",
        "https://ws.cn-hangzhou.maas.aliyuncs.com",
        "https://a.b.cn-beijing.maas.aliyuncs.com",
        "https://WS.cn-beijing.maas.aliyuncs.com",
        "https://_x.cn-beijing.maas.aliyuncs.com",
        "https://dashscope-intl.aliyuncs.com",
        "https://dashscope.aliyuncs.com\n",
        " https://dashscope.aliyuncs.com",
        "",
        "dashscope.aliyuncs.com",
    ],
)
def test_rejected_endpoints(url):
    assert not is_allowed_endpoint(url)


def test_aliyun_result_url_check():
    assert is_aliyun_https_url("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.json?x=1")
    assert not is_aliyun_https_url("https://aliyuncs.com/a.json")
    assert not is_aliyun_https_url("https://evilaliyuncs.com/a.json")
    assert not is_aliyun_https_url("http://x.aliyuncs.com/a.json")


def test_redact():
    text = (
        f"Authorization: Bearer {API_KEY} https://h.aliyuncs.com/a.json?Expires=1&Signature=S "
        "oss://bucket/key?x=1 sk-abcdef1234567890"
    )
    out = redact(text, (API_KEY,))
    assert API_KEY not in out
    assert "Signature" not in out
    assert "sk-abcdef1234567890" not in out
    assert "https://h.aliyuncs.com/a.json?<redacted>" in out


def test_safe_object_name():
    name = safe_object_name("会议 记录(1).M4A")
    assert name.isascii()
    assert name.endswith(".m4a")
    assert "/" not in safe_object_name("../../etc/passwd")
    assert safe_object_name("a.wav") != safe_object_name("a.wav")


# ---------------------------------------------------------------------------- request body
def _body(options: JobOptions, *, diarize: bool = True, channel_ids=(0,)):
    warnings: list[str] = []
    body = build_request_body(
        model=MODEL,
        file_url="oss://x/y.wav",
        options=options,
        channel_ids=list(channel_ids),
        diarize=diarize,
        warnings=warnings,
    )
    return body, warnings


def test_body_minimal_has_parameters():
    body, warnings = _body(JobOptions(language_hints=[]), diarize=False)
    assert body == {
        "model": MODEL,
        "input": {"file_urls": ["oss://x/y.wav"]},
        "parameters": {"channel_id": [0]},
    }
    assert warnings == []


def test_body_vocabulary_weights_and_duplicates():
    terms = [
        Term(term="CAM++", weight=3),
        Term(term="CAM++", weight=5),
        Term(term="Qwen3-ASR", weight=50),
        Term(term="FireRedASR"),
    ]
    body, _ = _body(JobOptions(terms=terms))
    assert body["parameters"]["vocabulary"] == {"CAM++": 5, "Qwen3-ASR": 50, "FireRedASR": 3}


def test_body_boosted_terms_capped():
    terms = [Term(term=f"t{i}", weight=50) for i in range(55)]
    body, warnings = _body(JobOptions(terms=terms))
    vocab = body["parameters"]["vocabulary"]
    assert sum(1 for w in vocab.values() if w == 50) == 50
    assert sum(1 for w in vocab.values() if w == 5) == 5
    assert any("boosted" in w for w in warnings)


def test_body_vocabulary_size_capped():
    terms = [Term(term=f"t{i}", weight=1) for i in range(2005)] + [Term(term="keep", weight=5)]
    body, warnings = _body(JobOptions(terms=terms))
    vocab = body["parameters"]["vocabulary"]
    assert len(vocab) == 2000
    assert vocab["keep"] == 5
    assert any("2000" in w for w in warnings)


def test_body_speaker_count_rules():
    assert _body(JobOptions(num_speakers=4))[0]["parameters"]["speaker_count"] == 4
    assert "speaker_count" not in _body(JobOptions())[0]["parameters"]
    assert "speaker_count" not in _body(JobOptions(num_speakers=4), diarize=False)[0]["parameters"]


def test_body_context_chunks():
    body, warnings = _body(JobOptions(context="甲" * 1000))
    messages = body["input"]["context"]
    assert [len(m["content"][0]["text"]) for m in messages] == [400, 400, 200]
    assert all(m["role"] == "user" and m["content"][0]["type"] == "input_text" for m in messages)
    assert warnings == []
    body, _ = _body(JobOptions(context="   "))
    assert "context" not in body["input"]


# -------------------------------------------------------------------------- normalisation
def test_normalize_ms_to_seconds_and_speakers():
    t = to_transcript([load("result_diarized.json")], per_channel=False, language="zh")
    assert t.duration == pytest.approx(15.2)
    assert all(0 <= s.start <= s.end <= t.duration for s in t.segments)
    assert len({s.id for s in t.segments}) == len(t.segments)
    assert {s.speaker_id for s in t.segments} == {sp.id for sp in t.speakers}
    assert [sp.name for sp in t.speakers] == [sp.id for sp in t.speakers]
    dumped = t.model_dump_json()
    for leaked in ("sentence_id", "channel_id", "oss://", "begin_time", "punctuation"):
        assert leaked not in dumped


def test_normalize_renumbers_by_first_appearance():
    doc = {
        "transcripts": [
            {
                "channel_id": 0,
                "sentences": [
                    {"begin_time": 5000, "end_time": 6000, "text": "b", "speaker_id": 0},
                    {"begin_time": 1000, "end_time": 2000, "text": "a", "speaker_id": 7},
                    {"begin_time": 3000, "end_time": 2500, "text": "c", "speaker_id": 7},
                    {"begin_time": 7000, "end_time": 8000, "text": "  ", "speaker_id": 3},
                    {"begin_time": 7000, "text": "no end", "speaker_id": 3},
                ],
            }
        ]
    }
    t = to_transcript([doc], per_channel=False, fallback_duration=None)
    assert [(s.text, s.speaker_id) for s in t.segments] == [
        ("a", "SPEAKER_00"),
        ("c", "SPEAKER_00"),
        ("b", "SPEAKER_01"),
    ]
    assert t.segments[1].start == t.segments[1].end == 3.0  # end < begin clamped
    assert t.duration == 6.0  # no properties: last segment end
    assert [s.id for s in t.segments] == ["seg-00000", "seg-00001", "seg-00002"]


def test_normalize_without_speaker_ids_is_single_speaker():
    doc = load("result_diarized.json")
    for s in doc["transcripts"][0]["sentences"]:
        s.pop("speaker_id")
    t = to_transcript([doc], per_channel=False)
    assert {s.speaker_id for s in t.segments} == {"SPEAKER_00"}
    assert len(t.speakers) == 1


def test_normalize_per_channel_one_speaker_per_channel():
    t = to_transcript([load("result_per_channel.json")], per_channel=True)
    assert [s.id for s in t.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert [s.speaker_id for s in t.segments] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert t.segments[0].words[0].text == "您好，"


def test_normalize_words_clamped_to_sentence():
    doc = {
        "transcripts": [
            {
                "sentences": [
                    {
                        "begin_time": 1000,
                        "end_time": 2000,
                        "text": "x",
                        "words": [
                            {"begin_time": 900, "end_time": 2100, "text": "x", "punctuation": ""},
                            {"begin_time": 1500, "end_time": 1600, "text": " ", "punctuation": ""},
                        ],
                    }
                ]
            }
        ]
    }
    (seg,) = to_transcript([doc], per_channel=False).segments
    assert [(w.start, w.end) for w in seg.words] == [(1.0, 2.0)]


def test_normalize_empty_result():
    t = to_transcript([{"transcripts": []}], per_channel=False, fallback_duration=12.5)
    assert t.segments == [] and t.speakers == [] and t.duration == 12.5


# ---------------------------------------------------------------------------- registry
def test_get_provider_cloud(tmp_path):
    p = get_provider("cloud", api_key=API_KEY, endpoint=ENDPOINT, model=MODEL, work_dir=tmp_path)
    assert isinstance(p, DashScopeFiletransProvider)
    assert p.name == "cloud"


def test_get_provider_local_and_unknown():
    with pytest.raises(ProviderError, match="M5"):
        get_provider("local")
    assert "M5" in LOCAL_UNAVAILABLE
    with pytest.raises(ProviderError):
        get_provider("other")  # type: ignore[arg-type]


def test_get_provider_bad_config(tmp_path):
    with pytest.raises(ProviderError, match="configuration"):
        get_provider("cloud", api_key=API_KEY)


# ------------------------------------------------------------------------- test_connection
def test_connection_ok():
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{ENDPOINT}/api/v1/uploads").mock(
            return_value=httpx.Response(200, json=load("get_policy.json"))
        )
        result = test_connection(api_key=API_KEY, endpoint=ENDPOINT, model=MODEL)
    assert result.ok and result.max_file_size_mb == 1024
    assert route.calls.last.request.url.params["model"] == MODEL
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"


def test_connection_rejected_key(caplog):
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{ENDPOINT}/api/v1/uploads").mock(
            return_value=httpx.Response(401, json={"code": "InvalidApiKey", "message": API_KEY})
        )
        result = test_connection(api_key=API_KEY, endpoint=ENDPOINT, model=MODEL)
    assert not result.ok
    assert "rejected the API key" in result.message
    assert API_KEY not in result.message and API_KEY not in caplog.text


def test_connection_no_key_no_network():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        result = test_connection(api_key="", endpoint=ENDPOINT, model=MODEL)
        assert not router.calls
    assert not result.ok and "key" in result.message


def test_connection_disallowed_endpoint_no_network():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        result = test_connection(api_key=API_KEY, endpoint="https://evil.example.com", model=MODEL)
        assert not router.calls
    assert not result.ok and "allow-list" in result.message


def test_connection_network_error():
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{ENDPOINT}/api/v1/uploads").mock(side_effect=httpx.ConnectError("boom"))
        result = test_connection(api_key=API_KEY, endpoint=ENDPOINT, model=MODEL)
    assert not result.ok
    assert API_KEY not in result.message


def test_connection_malformed_policy():
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{ENDPOINT}/api/v1/uploads").mock(
            return_value=httpx.Response(200, json={"data": {"policy": "x"}})
        )
        result = test_connection(api_key=API_KEY, endpoint=ENDPOINT, model=MODEL)
    assert not result.ok and "missing fields" in result.message
