"""DashScope filetrans provider end-to-end with respx (offline, no key)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from asr.providers.base import AudioSource, JobCancelled, JobOptions, ProviderError, Term
from tests.fixtures.dashscope.support import (
    API_KEY,
    MODEL,
    RESULT_PREFIX,
    SECRET_MARKERS,
    TASK_ID,
    FakeTranscode,
    Recorder,
    fake_probe,
    load,
    make_audio,
    make_provider,
    mock_dashscope,
    patch_audio,
    submitted_body,
    task_responses,
)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _options(**kw) -> JobOptions:
    return JobOptions(**kw)


# ------------------------------------------------------------------------------- happy path
def test_happy_path_temp_upload(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    audio = make_audio(tmp_path / "in", "会议 meeting.m4a")
    rec = Recorder()
    options = _options(
        num_speakers=4,
        terms=[Term(term="Kubernetes", weight=5), Term(term="Vue 3", weight=50)],
        context="项目周会：前端迁移、K8s 部署",
    )

    transcript = make_provider(work_dir).transcribe(AudioSource(path=audio), options, rec)

    # getPolicy: Bearer key, model bound.
    policy_req = routes.policy.calls.last.request
    assert policy_req.headers["Authorization"] == f"Bearer {API_KEY}"
    assert policy_req.url.params["action"] == "getPolicy"
    assert policy_req.url.params["model"] == MODEL

    # OSS PostObject form: policy fields, key under upload_dir, file last, no Authorization.
    up = routes.upload.calls.last.request
    assert "Authorization" not in up.headers
    assert up.headers["Content-Type"].startswith("multipart/form-data")
    content = up.content
    for field_name in (
        "OSSAccessKeyId",
        "policy",
        "Signature",
        "key",
        "x-oss-object-acl",
        "x-oss-forbid-overwrite",
        "success_action_status",
    ):
        assert f'name="{field_name}"'.encode() in content
    assert content.rfind(b'name="file"') > content.rfind(b'name="success_action_status"')
    assert b"\0" * 1024 in content  # original file uploaded unchanged
    assert fake.calls == []

    # Submit: headers and body.
    sub = routes.submit.calls.last.request
    assert sub.headers["Authorization"] == f"Bearer {API_KEY}"
    assert sub.headers["X-DashScope-Async"] == "enable"
    assert sub.headers["X-DashScope-OssResourceResolve"] == "enable"
    body = submitted_body(routes)
    assert body["model"] == MODEL
    (file_url,) = body["input"]["file_urls"]
    assert file_url.startswith("oss://dashscope-instant/acct0000/2026-10-29/abcd1234/")
    assert file_url.endswith(".m4a")
    assert file_url.split("/")[-1].isascii()
    params = body["parameters"]
    assert params["channel_id"] == [0]
    assert params["diarization_enabled"] is True
    assert params["speaker_count"] == 4
    assert params["vocabulary"] == {"Kubernetes": 5, "Vue 3": 50}
    assert params["language_hints"] == ["zh", "en"]
    assert "keep_dialect" not in params
    assert body["input"]["context"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "项目周会：前端迁移、K8s 部署"}],
        }
    ]

    # Result download: no credentials.
    res = routes.result.calls.last.request
    assert "Authorization" not in res.headers
    assert str(res.url).startswith(RESULT_PREFIX)

    # Progress and external id.
    assert rec.stages[0] == "preparing"
    for stage in ("uploading", "cloud_queued", "recognizing", "finalizing"):
        assert stage in rec.stages
    assert any(e.external_id == TASK_ID for e in rec.events)
    values = [e.progress for e in rec.events]
    assert values == sorted(values)

    # Transcript.
    assert transcript.provider == "cloud"
    assert [s.id for s in transcript.speakers] == [f"SPEAKER_0{i}" for i in range(4)]
    assert transcript.duration == pytest.approx(15.2)
    assert transcript.language is None
    starts = [s.start for s in transcript.segments]
    assert starts == sorted(starts)
    first = transcript.segments[0]
    assert (first.start, first.end) == (pytest.approx(0.1), pytest.approx(3.82))
    assert [w.text for w in first.words] == ["Hello", "world,", "这里是阿里巴巴语音实验室。"]
    assert first.words[1].start == pytest.approx(0.596)
    # Out-of-order sentence (speaker 2 at 6.6 s) is sorted before speaker 3 (9.8 s).
    assert [s.speaker_id for s in transcript.segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_02",
        "SPEAKER_03",
        "SPEAKER_00",
    ]
    assert transcript.segments[2].text.startswith("前端")
    assert transcript.warnings == []
    assert list(work_dir.iterdir()) == []


def test_url_source_is_used_as_is(router, work_dir):
    routes = mock_dashscope(router)
    url = "https://example.com/audio/meeting.m4a?token=abc"
    transcript = make_provider(work_dir, file_transfer="public_url").transcribe(
        AudioSource(url=url), _options(), Recorder()
    )
    assert not routes.policy.called
    assert not routes.upload.called
    sub = routes.submit.calls.last.request
    assert "X-DashScope-OssResourceResolve" not in sub.headers
    assert submitted_body(routes)["input"]["file_urls"] == [url]
    assert len(transcript.segments) == 5


def test_url_source_in_temp_upload_mode_follows_source(router, work_dir):
    routes = mock_dashscope(router)
    url = "https://example.com/a.wav"
    make_provider(work_dir).transcribe(AudioSource(url=url), _options(), Recorder())
    assert not routes.upload.called
    assert submitted_body(routes)["input"]["file_urls"] == [url]


def test_path_source_in_public_url_mode_uploads(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    make_provider(work_dir, file_transfer="public_url").transcribe(
        AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
    )
    assert routes.upload.called
    assert submitted_body(routes)["input"]["file_urls"][0].startswith("oss://")


def test_url_source_rejects_non_http(router, work_dir):
    with pytest.raises(ProviderError, match="http"):
        make_provider(work_dir).transcribe(
            AudioSource(url="file:///etc/passwd"), _options(), Recorder()
        )


def test_probe_runs_when_missing(router, work_dir, tmp_path, monkeypatch):
    seen: list[tuple[Path, int]] = []

    def probe(path, stream_index=0):
        seen.append((Path(path), stream_index))
        return fake_probe()

    from asr.audio import decode

    monkeypatch.setattr(decode, "probe", probe)
    patch_audio(monkeypatch)
    mock_dashscope(router)
    audio = make_audio(tmp_path)
    make_provider(work_dir).transcribe(AudioSource(path=audio), _options(), Recorder())
    assert seen == [(audio, 0)]


def test_supplied_probe_skips_probing(router, work_dir, tmp_path, monkeypatch):
    from asr.audio import decode

    def boom(*a, **k):
        raise AssertionError("probe must not run")

    monkeypatch.setattr(decode, "probe", boom)
    patch_audio(monkeypatch)
    mock_dashscope(router)
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path), probe=fake_probe()), _options(), Recorder()
    )


def test_supplied_probe_of_another_stream_is_reprobed(router, work_dir, tmp_path, monkeypatch):
    """The server stores a stream-0 probe; a job on stream 1 must probe stream 1 itself."""
    from asr.audio import decode

    seen: list[int] = []

    def probe(path, stream_index=0):
        seen.append(stream_index)
        return fake_probe(stream_index=stream_index, audio_streams=2)

    monkeypatch.setattr(decode, "probe", probe)
    fake = patch_audio(monkeypatch)
    mock_dashscope(router)
    options = _options()
    options.preprocess.stream_index = 1
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path), probe=fake_probe(audio_streams=2)),
        options,
        Recorder(),
    )
    assert seen == [1]
    assert fake.calls[0]["stream_index"] == 1


# -------------------------------------------------------------------------- D8 preparation
def test_downmix_stereo_transcodes_to_mono_flac(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(
        monkeypatch, probe=fake_probe(channels=2, channel_policy="downmix", channel_correlation=0.9)
    )
    routes = mock_dashscope(router)
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path, "rec.m4a")), _options(), Recorder()
    )
    assert isinstance(fake, FakeTranscode)
    (call,) = fake.calls
    assert call["fmt"] == "flac"
    assert call["sample_rate"] == 16000
    assert call["channels"] == 1
    assert call["dst"].parent == work_dir
    assert submitted_body(routes)["parameters"]["channel_id"] == [0]
    assert submitted_body(routes)["input"]["file_urls"][0].endswith(".flac")
    assert b"\1" * 512 in routes.upload.calls.last.request.content
    assert list(work_dir.iterdir()) == []  # temp FLAC removed


def test_over_cap_transcodes_keeping_layout(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router, policy={"max_file_size_mb": "1"})  # string, as documented
    audio = make_audio(tmp_path, "big.wav", size=2 * 1024 * 1024)
    make_provider(work_dir).transcribe(AudioSource(path=audio), _options(), Recorder())
    (call,) = fake.calls
    assert call["fmt"] == "flac" and call["channels"] is None and call["sample_rate"] == 16000
    assert routes.upload.called
    assert list(work_dir.iterdir()) == []


def test_mono_under_cap_is_not_transcoded(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(monkeypatch, probe=fake_probe())
    mock_dashscope(router)
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
    )
    assert fake.calls == []


def test_cap_exceeded_after_transcode(router, work_dir, tmp_path, monkeypatch):
    big = 3 * 1024 * 1024
    fake = patch_audio(
        monkeypatch, probe=fake_probe(), transcode=FakeTranscode(sizes={"flac": big, "mp3": big})
    )
    routes = mock_dashscope(router, policy={"max_file_size_mb": 1})
    audio = make_audio(tmp_path, "huge.wav", size=2 * 1024 * 1024)
    with pytest.raises(ProviderError, match=r"1 MB"):
        make_provider(work_dir).transcribe(AudioSource(path=audio), _options(), Recorder())
    assert [c["fmt"] for c in fake.calls] == ["flac", "mp3"]
    assert not routes.upload.called
    assert not routes.submit.called
    assert list(work_dir.iterdir()) == []


def test_mp3_fallback_when_flac_too_big(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(
        monkeypatch,
        probe=fake_probe(),
        transcode=FakeTranscode(sizes={"flac": 3 * 1024 * 1024, "mp3": 1000}),
    )
    routes = mock_dashscope(router, policy={"max_file_size_mb": 1})
    audio = make_audio(tmp_path, "huge.wav", size=2 * 1024 * 1024)
    make_provider(work_dir).transcribe(AudioSource(path=audio), _options(), Recorder())
    assert [c["fmt"] for c in fake.calls] == ["flac", "mp3"]
    assert submitted_body(routes)["input"]["file_urls"][0].endswith(".mp3")


def test_per_channel_keeps_stereo_without_diarization(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(
        monkeypatch,
        probe=fake_probe(channels=2, channel_policy="per_channel", channel_correlation=0.02),
    )
    routes = mock_dashscope(router, result="result_per_channel.json")
    transcript = make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path, "call.wav")), _options(num_speakers=2), Recorder()
    )
    assert fake.calls == []
    params = submitted_body(routes)["parameters"]
    assert params["channel_id"] == [0, 1]
    assert "diarization_enabled" not in params
    assert "speaker_count" not in params
    assert [s.id for s in transcript.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert [(s.speaker_id, s.start) for s in transcript.segments] == [
        ("SPEAKER_00", 0.0),
        ("SPEAKER_01", 2.1),
        ("SPEAKER_00", 5.0),
    ]
    assert transcript.duration == pytest.approx(8.0)


def test_per_channel_over_cap_transcodes_stereo(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="downmix"))
    routes = mock_dashscope(
        router, policy={"max_file_size_mb": 1}, result="result_per_channel.json"
    )
    audio = make_audio(tmp_path, "call.wav", size=2 * 1024 * 1024)
    options = _options()
    options.preprocess.channels = "per_channel"
    make_provider(work_dir).transcribe(AudioSource(path=audio), options, Recorder())
    (call,) = fake.calls
    assert call["channels"] is None  # keeps both channels
    assert submitted_body(routes)["parameters"]["channel_id"] == [0, 1]


def test_select_right_channel(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(monkeypatch, probe=fake_probe(channels=2, channel_policy="downmix"))
    routes = mock_dashscope(router)
    options = _options()
    options.preprocess.channels = "right"
    make_provider(work_dir).transcribe(AudioSource(path=make_audio(tmp_path)), options, Recorder())
    (call,) = fake.calls  # extracted to mono: DashScope diarizes mono audio only
    assert call["channel"] == 1 and call["channels"] == 1
    params = submitted_body(routes)["parameters"]
    assert params["channel_id"] == [0]
    assert params["diarization_enabled"] is True


def test_non_default_stream_is_extracted(router, work_dir, tmp_path, monkeypatch):
    fake = patch_audio(monkeypatch, probe=fake_probe(stream_index=1, audio_streams=2))
    mock_dashscope(router)
    options = _options()
    options.preprocess.stream_index = 1
    make_provider(work_dir).transcribe(AudioSource(path=make_audio(tmp_path)), options, Recorder())
    (call,) = fake.calls
    assert call["stream_index"] == 1


def test_invalid_channel_option_is_provider_error(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe(channels=1))
    mock_dashscope(router)
    options = _options()
    options.preprocess.channels = "right"
    with pytest.raises(ProviderError, match="stereo"):
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)), options, Recorder()
        )


def test_single_speaker_disables_diarization(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)), _options(num_speakers=1), Recorder()
    )
    params = submitted_body(routes)["parameters"]
    assert "diarization_enabled" not in params and "speaker_count" not in params


def test_long_audio_with_diarization_warns(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe(duration=3 * 3600.0))
    mock_dashscope(router)
    transcript = make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
    )
    assert any("2 hours" in w for w in transcript.warnings)
    assert transcript.duration == pytest.approx(15.2)  # the result's own duration wins


def test_keep_dialect_and_language_hints(router, work_dir, monkeypatch):
    routes = mock_dashscope(router)
    options = _options(language_hints=["zh", "en", "ja", "ko", "fr"])
    options.cloud.keep_dialect = True
    transcript = make_provider(work_dir).transcribe(
        AudioSource(url="https://example.com/a.mp3"), options, Recorder()
    )
    params = submitted_body(routes)["parameters"]
    assert params["keep_dialect"] is True
    assert params["language_hints"] == ["zh", "en", "ja", "ko"]
    assert any("language hints" in w for w in transcript.warnings)


# -------------------------------------------------------------------------- failures
def test_failed_task(router, work_dir):
    mock_dashscope(router, tasks=task_responses("task_running.json", "task_failed.json"))
    with pytest.raises(ProviderError, match=r"InvalidFile\.DecodeFailed"):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )


def test_failed_subtask(router, work_dir):
    routes = mock_dashscope(router, tasks=task_responses("task_subtask_failed.json"))
    with pytest.raises(ProviderError, match="FILE_DOWNLOAD_FAILED"):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )
    assert not routes.result.called


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejected(router, work_dir, tmp_path, monkeypatch, status, caplog):
    caplog.set_level(logging.DEBUG)
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    routes.policy.mock(
        return_value=httpx.Response(
            status,
            json={"code": "InvalidApiKey", "message": f"Invalid API-key provided: {API_KEY}"},
        )
    )
    with pytest.raises(ProviderError) as info:
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
        )
    assert "rejected the API key" in str(info.value)
    assert API_KEY not in str(info.value)
    assert API_KEY not in caplog.text
    assert routes.policy.call_count == 1  # never retried


def test_submit_4xx_message_is_redacted(router, work_dir):
    routes = mock_dashscope(router)
    routes.submit.mock(
        return_value=httpx.Response(
            400,
            json={
                "code": "InvalidParameter",
                "message": (
                    "bad url https://x.aliyuncs.com/f?Signature=SIGNATURE_PLACEHOLDER "
                    f"key {API_KEY}"
                ),
            },
        )
    )
    with pytest.raises(ProviderError) as info:
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )
    text = str(info.value)
    assert "InvalidParameter" in text
    for marker in SECRET_MARKERS:
        assert marker not in text


def test_429_on_poll_is_retried(router, work_dir):
    throttled = httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "Throttling"})
    routes = mock_dashscope(
        router,
        tasks=[
            throttled,
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
            *task_responses("task_running.json", "task_succeeded.json"),
        ],
    )
    transcript = make_provider(work_dir).transcribe(
        AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
    )
    assert routes.task.call_count == 5
    assert len(transcript.segments) == 5


def test_poll_retries_exhausted(router, work_dir):
    routes = mock_dashscope(router, tasks=[httpx.Response(500)] * 10)
    provider = make_provider(work_dir)
    provider.max_retries = 2
    with pytest.raises(ProviderError, match="HTTP 500"):
        provider.transcribe(AudioSource(url="https://example.com/a.mp3"), _options(), Recorder())
    assert routes.task.call_count == 3


def test_submit_retries_429_but_not_500(router, work_dir):
    routes = mock_dashscope(router)
    routes.submit.mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=load("submit.json"))]
    )
    make_provider(work_dir).transcribe(
        AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
    )
    assert routes.submit.call_count == 2

    routes.submit.reset()
    routes.submit.mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=load("submit.json"))]
    )
    with pytest.raises(ProviderError, match="HTTP 500"):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )
    assert routes.submit.call_count == 1


def test_cancellation_mid_poll_cancels_remote_task(router, work_dir):
    routes = mock_dashscope(router)
    with pytest.raises(JobCancelled):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"),
            _options(),
            Recorder(cancel_on="recognizing"),
        )
    assert routes.cancel.called
    assert routes.cancel.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert not routes.result.called


def test_cancellation_when_cancel_endpoint_fails(router, work_dir):
    routes = mock_dashscope(router)
    routes.cancel.mock(return_value=httpx.Response(400, json={"code": "UnsupportedOperation"}))
    with pytest.raises(JobCancelled):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"),
            _options(),
            Recorder(cancel_on="cloud_queued"),
        )
    assert routes.cancel.called


def test_cancellation_during_upload(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    with pytest.raises(JobCancelled):
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)), _options(), Recorder(cancel_on="uploading")
        )
    assert not routes.submit.called
    assert not routes.cancel.called


def test_overall_timeout_cancels(router, work_dir):
    routes = mock_dashscope(router, tasks=task_responses(*["task_running.json"] * 3))
    provider = make_provider(work_dir, timeout_s=0.0)
    with pytest.raises(ProviderError, match="did not finish"):
        provider.transcribe(AudioSource(url="https://example.com/a.mp3"), _options(), Recorder())
    assert routes.cancel.called


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/result.json",
        "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/r.json",
        "https://aliyuncs.com.evil.example/r.json",
        "https://user@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/r.json",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com:8443/r.json",
        "file:///etc/passwd",
    ],
)
def test_result_url_host_rejected(router, work_dir, url):
    done = load("task_succeeded.json")
    done["output"]["results"][0]["transcription_url"] = url
    routes = mock_dashscope(router, succeeded=done)
    with pytest.raises(ProviderError, match="unexpected host"):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )
    assert not routes.result.called


def test_upload_host_must_be_aliyun(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router, policy={"upload_host": "https://attacker.example.com"})
    with pytest.raises(ProviderError, match="upload host"):
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
        )
    assert not routes.upload.called


def test_upload_rejected_by_oss(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    routes.upload.mock(return_value=httpx.Response(403, text="<Error>Policy expired</Error>"))
    with pytest.raises(ProviderError, match="HTTP 403"):
        make_provider(work_dir).transcribe(
            AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
        )


def test_upload_retry_uses_fresh_policy_and_key(router, work_dir, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(router)
    routes.upload.mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
    )
    assert routes.upload.call_count == 2
    assert routes.policy.call_count == 2

    def key_of(req: httpx.Request) -> bytes:
        content = req.content
        start = content.index(b'name="key"') + len(b'name="key"\r\n\r\n')
        return content[start : content.index(b"\r\n", start)]

    keys = [key_of(c.request) for c in routes.upload.calls]
    assert keys[0] != keys[1]
    assert submitted_body(routes)["input"]["file_urls"] == [f"oss://{keys[1].decode()}"]


def test_secrets_never_logged(router, work_dir, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    patch_audio(monkeypatch, probe=fake_probe())
    routes = mock_dashscope(
        router,
        tasks=[httpx.Response(503), *task_responses("task_running.json", "task_succeeded.json")],
    )
    make_provider(work_dir).transcribe(
        AudioSource(path=make_audio(tmp_path)), _options(), Recorder()
    )
    assert routes.result.called
    for marker in SECRET_MARKERS:
        assert marker not in caplog.text
    assert "Signature=" not in caplog.text
    assert "?<redacted>" in caplog.text  # httpx request log lines were redacted, not dropped


def test_provider_repr_hides_key(work_dir):
    assert API_KEY not in repr(make_provider(work_dir))


def test_constructor_rejects_bad_config(work_dir):
    with pytest.raises(ProviderError, match="allow-list"):
        make_provider(work_dir, endpoint="https://evil.example.com")
    with pytest.raises(ProviderError, match="API key"):
        make_provider(work_dir, api_key="")


def test_endpoint_trailing_slash_and_workspace(router, work_dir):
    ws = "https://ws-abc123.cn-beijing.maas.aliyuncs.com"
    router.post(f"{ws}/api/v1/services/audio/asr/transcription").mock(
        return_value=httpx.Response(200, json=load("submit.json"))
    )
    router.get(f"{ws}/api/v1/tasks/{TASK_ID}").mock(
        side_effect=task_responses("task_succeeded.json")
    )
    router.get(url__startswith=RESULT_PREFIX).mock(
        return_value=httpx.Response(200, json=load("result_diarized.json"))
    )
    transcript = make_provider(work_dir, endpoint=f"{ws}/").transcribe(
        AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
    )
    assert len(transcript.segments) == 5


def test_submit_without_task_id(router, work_dir):
    routes = mock_dashscope(router)
    routes.submit.mock(return_value=httpx.Response(200, json={"output": {}}))
    with pytest.raises(ProviderError, match="task id"):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )


def test_result_invalid_json(router, work_dir):
    routes = mock_dashscope(router)
    routes.result.mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(ProviderError, match="invalid JSON"):
        make_provider(work_dir).transcribe(
            AudioSource(url="https://example.com/a.mp3"), _options(), Recorder()
        )


def test_fixtures_match_documented_shape():
    result = load("result_diarized.json")
    sentence = result["transcripts"][0]["sentences"][0]
    assert {"begin_time", "end_time", "text", "sentence_id", "speaker_id", "words"} <= set(sentence)
    assert {"begin_time", "end_time", "text", "punctuation"} <= set(sentence["words"][0])
    assert "original_duration_in_milliseconds" in result["properties"]
    task = load("task_succeeded.json")["output"]
    assert {"task_id", "task_status", "results", "task_metrics"} <= set(task)
    assert json.dumps(load("get_policy.json")["data"]).count("PLACEHOLDER") == 3


def _write_wav(path: Path, *, channels: int, seconds: float = 2.0, rate: int = 44100) -> Path:
    import math
    import struct
    import wave

    frames = bytearray()
    for i in range(int(seconds * rate)):
        v = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
        frames += struct.pack("<" + "h" * channels, *([v] * channels))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def test_real_audio_downmix_uploads_flac(router, work_dir, tmp_path):
    """No monkeypatching: real probe + real 16 kHz mono FLAC transcode (D8)."""
    routes = mock_dashscope(router)
    audio = _write_wav(tmp_path / "stereo.wav", channels=2)
    make_provider(work_dir).transcribe(AudioSource(path=audio), _options(), Recorder())
    content = routes.upload.calls.last.request.content
    assert b"fLaC" in content
    assert len(content) < audio.stat().st_size
    assert submitted_body(routes)["input"]["file_urls"][0].endswith(".flac")
    assert submitted_body(routes)["parameters"]["channel_id"] == [0]
    assert list(work_dir.iterdir()) == []
