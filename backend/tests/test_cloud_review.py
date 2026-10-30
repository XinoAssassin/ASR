"""Cloud provider review regressions (respx, no key, no network): per_channel diarizes channel 0
(m1), URL channel selection (m2), language hints (m3), result-download 403 (n4), lazy speaker ids
(n5)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
import respx

from asr.providers.base import AudioSource, JobCancelled, JobOptions, ProviderError
from asr.providers.cloud.dashscope_filetrans import build_request_body
from asr.providers.cloud.normalize import ResultTrack, to_transcript, tracks_to_transcript
from tests.fixtures.dashscope.support import (
    ENDPOINT,
    RESULT_PREFIX,
    UPLOAD_HOST,
    FakeTranscode,
    Recorder,
    fake_probe,
    load,
    make_audio,
    make_provider,
    mock_dashscope,
    patch_audio,
    submitted_body,
)

TASK_A = "aaaaaaaa-0000-0000-0000-00000000000a"
TASK_B = "bbbbbbbb-0000-0000-0000-00000000000b"
SUBMIT = f"{ENDPOINT}/api/v1/services/audio/asr/transcription"


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _mono_result(sentences: list[tuple[int, int, str]], duration_ms: int = 15200) -> dict:
    return {
        "properties": {"channels": [0], "original_duration_in_milliseconds": duration_ms},
        "transcripts": [
            {
                "channel_id": 0,
                "sentences": [
                    {"begin_time": a, "end_time": b, "text": t, "sentence_id": i, "words": []}
                    for i, (a, b, t) in enumerate(sentences)
                ],
            }
        ],
    }


LOCAL_MIC = _mono_result([(3900, 4000, "嗯。"), (7000, 8000, "我这边听得到。")])


def _succeeded(task_id: str) -> dict:
    done = load("task_succeeded.json")
    done["output"]["task_id"] = task_id
    done["output"]["results"][0]["transcription_url"] = f"{RESULT_PREFIX}{task_id}.json?Signature=x"
    return done


def _mock_two_tasks(router: respx.MockRouter, results: dict[str, dict]) -> dict[str, respx.Route]:
    router.get(f"{ENDPOINT}/api/v1/uploads").mock(
        return_value=httpx.Response(200, json=load("get_policy.json"))
    )
    upload = router.post(UPLOAD_HOST).mock(return_value=httpx.Response(200))
    submit = router.post(SUBMIT).mock(
        side_effect=[
            httpx.Response(200, json={"output": {"task_id": t, "task_status": "PENDING"}})
            for t in (TASK_A, TASK_B)
        ]
    )
    out = {"submit": submit, "upload": upload}
    for task_id in (TASK_A, TASK_B):
        out[f"task_{task_id}"] = router.get(f"{ENDPOINT}/api/v1/tasks/{task_id}").mock(
            side_effect=[
                httpx.Response(
                    200, json={"output": {"task_id": task_id, "task_status": "RUNNING"}}
                ),
                httpx.Response(200, json=_succeeded(task_id)),
            ]
        )
        out[f"cancel_{task_id}"] = router.post(f"{ENDPOINT}/api/v1/tasks/{task_id}/cancel").mock(
            return_value=httpx.Response(200, json={})
        )
        out[f"result_{task_id}"] = router.get(url__startswith=f"{RESULT_PREFIX}{task_id}").mock(
            return_value=httpx.Response(200, json=copy.deepcopy(results[task_id]))
        )
    return out


def _per_channel_options(**kw) -> JobOptions:
    options = JobOptions(**kw)
    options.preprocess.channels = "per_channel"
    return options


# ---------------------------------------------------------------------------------------- m1
def test_per_channel_diarizes_channel_zero_and_fixes_channel_one(
    router, work_dir, tmp_path, monkeypatch
):
    fake = patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="per_channel"))
    routes = _mock_two_tasks(router, {TASK_A: load("result_diarized.json"), TASK_B: LOCAL_MIC})
    rec = Recorder()
    transcript = make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path, "desktop.wav")), _per_channel_options(), rec
    )

    # Each channel extracted to its own mono FLAC.
    assert isinstance(fake, FakeTranscode)
    assert [(c["channel"], c["channels"], c["fmt"]) for c in fake.calls] == [
        (0, 1, "flac"),
        (1, 1, "flac"),
    ]
    assert routes["upload"].call_count == 2
    bodies = [json.loads(c.request.content) for c in routes["submit"].calls]
    assert [b["parameters"]["channel_id"] for b in bodies] == [[0], [0]]
    assert bodies[0]["parameters"]["diarization_enabled"] is True
    assert "speaker_count" not in bodies[0]["parameters"]  # auto
    assert "diarization_enabled" not in bodies[1]["parameters"]
    assert bodies[0]["input"]["file_urls"][0].endswith(".flac")

    # Remote speakers 0..3 on ch 0, then the local mic as the next id; merged by time.
    assert [s.id for s in transcript.speakers] == [f"SPEAKER_0{i}" for i in range(5)]
    assert [(s.start, s.speaker_id) for s in transcript.segments] == [
        (0.1, "SPEAKER_00"),
        (3.9, "SPEAKER_04"),
        (4.0, "SPEAKER_01"),
        (6.6, "SPEAKER_02"),
        (7.0, "SPEAKER_04"),
        (9.8, "SPEAKER_03"),
        (12.1, "SPEAKER_00"),
    ]
    assert transcript.duration == pytest.approx(15.2)
    assert transcript.warnings == []

    # One progress stream: monotonic, both task ids reported, no temp files left.
    values = [e.progress for e in rec.events]
    assert values == sorted(values)
    assert any(e.external_id == f"{TASK_A},{TASK_B}" for e in rec.events)
    assert list(work_dir.iterdir()) == []


def test_per_channel_speaker_count_excludes_the_fixed_channel(
    router, work_dir, tmp_path, monkeypatch
):
    patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="per_channel"))
    routes = _mock_two_tasks(router, {TASK_A: load("result_diarized.json"), TASK_B: LOCAL_MIC})
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)), _per_channel_options(num_speakers=5), Recorder()
    )
    first = json.loads(routes["submit"].calls[0].request.content)["parameters"]
    assert first["diarization_enabled"] is True and first["speaker_count"] == 4


@pytest.mark.parametrize("num_speakers", [1, 2])
def test_per_channel_without_room_for_more_speakers_is_one_task(
    router, work_dir, tmp_path, monkeypatch, num_speakers
):
    fake = patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="per_channel"))
    routes = mock_dashscope(router, result="result_per_channel.json")
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)),
        _per_channel_options(num_speakers=num_speakers),
        Recorder(),
    )
    assert fake.calls == [] and routes.submit.call_count == 1
    params = submitted_body(routes)["parameters"]
    assert params["channel_id"] == [0, 1] and "diarization_enabled" not in params


def test_per_channel_cancel_cancels_both_tasks(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="per_channel"))
    routes = _mock_two_tasks(router, {TASK_A: load("result_diarized.json"), TASK_B: LOCAL_MIC})
    with pytest.raises(JobCancelled):
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)),
            _per_channel_options(),
            Recorder(cancel_on="recognizing"),
        )
    assert routes[f"cancel_{TASK_A}"].called and routes[f"cancel_{TASK_B}"].called


def test_per_channel_second_submit_failure_cancels_the_first(
    router, work_dir, tmp_path, monkeypatch
):
    patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="per_channel"))
    routes = _mock_two_tasks(router, {TASK_A: load("result_diarized.json"), TASK_B: LOCAL_MIC})
    routes["submit"].mock(
        side_effect=[
            httpx.Response(200, json={"output": {"task_id": TASK_A, "task_status": "PENDING"}}),
            httpx.Response(400, json={"code": "InvalidParameter", "message": "bad"}),
        ]
    )
    with pytest.raises(ProviderError, match="InvalidParameter"):
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)), _per_channel_options(), Recorder()
        )
    assert routes[f"cancel_{TASK_A}"].called


def test_per_channel_url_warns_that_channel_zero_is_not_diarized(router, work_dir):
    routes = mock_dashscope(router, result="result_per_channel.json")
    transcript = make_provider(work_dir).transcribe(
        AudioSource(url="https://example.com/call.wav"), _per_channel_options(), Recorder()
    )
    params = submitted_body(routes)["parameters"]
    assert params["channel_id"] == [0, 1] and "diarization_enabled" not in params
    assert any("channel 0" in w for w in transcript.warnings)


# ---------------------------------------------------------------------------------------- m2
@pytest.mark.parametrize(("option", "channel"), [("right", 1), ("left", 0)])
def test_url_selected_channel_is_not_diarized(router, work_dir, option, channel):
    routes = mock_dashscope(router)
    options = JobOptions()
    options.preprocess.channels = option
    transcript = make_provider(work_dir).transcribe(
        AudioSource(url="https://example.com/a.wav"), options, Recorder()
    )
    params = submitted_body(routes)["parameters"]
    assert params["channel_id"] == [channel]
    assert "diarization_enabled" not in params
    assert any("mono" in w for w in transcript.warnings)


def test_url_auto_still_diarizes(router, work_dir):
    routes = mock_dashscope(router)
    make_provider(work_dir).transcribe(
        AudioSource(url="https://example.com/a.mp3"), JobOptions(), Recorder()
    )
    assert submitted_body(routes)["parameters"]["diarization_enabled"] is True


# ---------------------------------------------------------------------------------------- m3
@pytest.mark.parametrize(
    ("hints", "sent"),
    [
        (["zh-CN", "en-US"], ["zh", "en"]),
        (["auto"], None),
        (["Chinese", "zh_Hans", "zh"], ["zh"]),
        (["klingon", "ja"], ["ja"]),
    ],
)
def test_language_hints_are_normalised_like_local(hints, sent):
    body = build_request_body(
        model="m",
        file_url="https://example.com/a.mp3",
        options=JobOptions(language_hints=hints),
        channel_ids=[0],
        diarize=False,
        warnings=[],
    )
    assert body["parameters"].get("language_hints") == sent


# ---------------------------------------------------------------------------------------- n4
@pytest.mark.parametrize("status", [401, 403])
def test_forbidden_result_url_is_not_reported_as_a_bad_key(router, work_dir, status):
    routes = mock_dashscope(router)
    routes.result.mock(return_value=httpx.Response(status, text="<Error>AccessDenied</Error>"))
    with pytest.raises(ProviderError) as info:
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), JobOptions(), Recorder()
        )
    assert "API key" not in str(info.value)
    assert "result download" in str(info.value) and str(status) in str(info.value)


# ---------------------------------------------------------------------------------------- n5
def test_track_numbering_skips_silent_channels():
    # Channel 0 of a per-channel task has no sentences: channel 1 is SPEAKER_00, as locally.
    doc = load("result_per_channel.json")
    doc["transcripts"] = [t for t in doc["transcripts"] if t["channel_id"] == 1]
    t = to_transcript([doc], per_channel=True)
    assert {s.speaker_id for s in t.segments} == {"SPEAKER_00"}


def test_tracks_number_in_track_order_then_first_appearance():
    silent = _mono_result([])
    t = tracks_to_transcript(
        [
            ResultTrack(results=[silent], per_channel=False),  # ch 0 said nothing
            ResultTrack(results=[LOCAL_MIC], per_channel=True),
        ]
    )
    assert [s.id for s in t.speakers] == ["SPEAKER_00"]
    assert {s.speaker_id for s in t.segments} == {"SPEAKER_00"}
