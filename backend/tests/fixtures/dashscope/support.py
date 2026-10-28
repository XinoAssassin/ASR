"""Shared helpers for the offline DashScope tests (respx; no key, no network).

The JSON files next to this module are modelled on the documented response shapes
(see ``asr.providers.cloud.dashscope_filetrans`` docstring for the source URLs).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import respx

from asr.audio.types import ProbeInfo
from asr.providers.base import JobCancelled, ProgressEvent
from asr.providers.cloud import DashScopeFiletransProvider

FIXTURES = Path(__file__).parent
ENDPOINT = "https://dashscope.aliyuncs.com"
API_KEY = "sk-0123456789abcdeftestkey"
MODEL = "qwen-audio-3.1-asr-flash-filetrans"
TASK_ID = "c2e5d63b-96e1-4607-bb91-000000000001"
UPLOAD_HOST = "https://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com"
RESULT_PREFIX = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/"
SECRET_MARKERS = (
    API_KEY,
    "SIGNATURE_PLACEHOLDER",
    "ACCESS_KEY_ID_PLACEHOLDER",
    "POLICY_PLACEHOLDER",
)
KNOWN_STAGES = frozenset(
    {
        "queued",
        "preparing",
        "uploading",
        "cloud_queued",
        "recognizing",
        "finalizing",
        "done",
        "decoding",
        "vad",
        "asr",
        "aligning",
        "diarizing",
        "merging",
    }
)


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fake_probe(**overrides: Any) -> ProbeInfo:
    data: dict[str, Any] = {
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "codec": "aac",
        "duration": 15.2,
        "sample_rate": 48000,
        "channels": 1,
        "bit_rate": 64000,
        "stream_index": 0,
        "audio_streams": 1,
        "size": 1024,
        "channel_policy": "mono",
        "channel_correlation": None,
    }
    data.update(overrides)
    return ProbeInfo(**data)


def make_audio(directory: Path, name: str = "meeting.m4a", size: int = 1024) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\0" * size)
    return path


def make_provider(work_dir: Path, **kwargs: Any) -> DashScopeFiletransProvider:
    params: dict[str, Any] = {
        "api_key": API_KEY,
        "endpoint": ENDPOINT,
        "model": MODEL,
        "work_dir": work_dir,
        "poll_interval": 0.0,
    }
    params.update(kwargs)
    provider = DashScopeFiletransProvider(**params)
    provider.retry_backoff = 0.0
    return provider


@dataclass
class Recorder:
    """Progress callback that records events and can raise ``JobCancelled`` on a stage."""

    cancel_on: str | None = None
    events: list[ProgressEvent] = field(default_factory=list)

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)
        if self.cancel_on is not None and event.stage == self.cancel_on:
            raise JobCancelled

    @property
    def stages(self) -> list[str]:
        return [e.stage for e in self.events]


@dataclass
class Routes:
    policy: respx.Route
    upload: respx.Route
    submit: respx.Route
    task: respx.Route
    cancel: respx.Route
    result: respx.Route


def task_responses(*names: str) -> list[httpx.Response]:
    return [httpx.Response(200, json=load(n)) for n in names]


def mock_dashscope(
    router: respx.MockRouter,
    *,
    policy: dict[str, Any] | None = None,
    tasks: list[Any] | None = None,
    result: str | dict[str, Any] = "result_diarized.json",
    succeeded: dict[str, Any] | None = None,
) -> Routes:
    """Register every DashScope / OSS route used by one ``transcribe`` call."""
    policy_body = load("get_policy.json")
    if policy:
        policy_body["data"].update(policy)
    done = succeeded or load("task_succeeded.json")
    sequence = (
        tasks
        if tasks is not None
        else [
            *task_responses("task_pending.json", "task_running.json"),
            httpx.Response(200, json=done),
        ]
    )
    result_body = load(result) if isinstance(result, str) else result
    return Routes(
        policy=router.get(f"{ENDPOINT}/api/v1/uploads").mock(
            return_value=httpx.Response(200, json=policy_body)
        ),
        upload=router.post(UPLOAD_HOST).mock(return_value=httpx.Response(200)),
        submit=router.post(f"{ENDPOINT}/api/v1/services/audio/asr/transcription").mock(
            return_value=httpx.Response(200, json=load("submit.json"))
        ),
        task=router.get(f"{ENDPOINT}/api/v1/tasks/{TASK_ID}").mock(side_effect=sequence),
        cancel=router.post(f"{ENDPOINT}/api/v1/tasks/{TASK_ID}/cancel").mock(
            return_value=httpx.Response(200, json={"request_id": "r"})
        ),
        result=router.get(url__startswith=RESULT_PREFIX).mock(
            return_value=httpx.Response(200, json=copy.deepcopy(result_body))
        ),
    )


@dataclass
class FakeTranscode:
    """Stand-in for ``asr.audio.transcode.transcode_audio`` writing ``sizes[fmt]`` bytes."""

    sizes: dict[str, int] = field(default_factory=lambda: {"flac": 512, "mp3": 256})
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, src: Path, dst: Path, **kwargs: Any) -> Path:
        self.calls.append({"src": Path(src), "dst": Path(dst), **kwargs})
        Path(dst).write_bytes(b"\1" * self.sizes[kwargs["fmt"]])
        return Path(dst)


def submitted_body(routes: Routes) -> dict[str, Any]:
    return json.loads(routes.submit.calls.last.request.content)


def patch_audio(
    monkeypatch: Any,
    *,
    probe: ProbeInfo | None = None,
    transcode: Callable[..., Path] | None = None,
) -> FakeTranscode | Callable[..., Path]:
    """Monkeypatch ``asr.audio.decode.probe`` and ``asr.audio.transcode.transcode_audio``."""
    from asr.audio import decode
    from asr.audio import transcode as transcode_mod

    if probe is not None:
        monkeypatch.setattr(decode, "probe", lambda path, stream_index=0: probe)
    fake = transcode or FakeTranscode()
    monkeypatch.setattr(transcode_mod, "transcode_audio", fake)
    return fake
