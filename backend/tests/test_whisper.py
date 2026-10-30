"""M11 faster-whisper English route: registry, engine wrapper (fake model), pipeline, job route."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from asr.providers.base import AudioSource, JobOptions
from asr.providers.local import models
from asr.providers.local.asr_whisper import WhisperAsr, compute_type, strip_punct
from asr.providers.local.pipeline import LocalEngines, LocalPipelineProvider
from server.config import EnvSettings
from tests.fixtures.dashscope.support import Recorder
from tests.test_audio_fixtures import write_audio
from tests.test_jobs import FakeProvider, add_upload, install
from tests.test_local_pipeline import FakeDiarizer, FakeVad
from tests.test_routes_jobs import _fake_models

WHISPER = "faster-whisper-small"


def test_registry_entries() -> None:
    for key in (WHISPER, "faster-whisper-large-v3-turbo"):
        s = models.spec(key)
        assert (s.kind, s.engine, s.marker) == ("asr", "faster_whisper", "model.bin")
        assert s.mirror
        assert models.emits_timestamps(key)
        assert models.required_models(key) == ["fsmn-vad", key, "campplus"]  # no aligner
    assert "qwen3-forced-aligner" in models.required_models("qwen3-asr-0.6b")
    assert models.engine_installed("qwen3-asr-0.6b")


def test_engine_installed_follows_package(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "faster_whisper" else real(name, *a),
    )
    assert not models.engine_installed(WHISPER)
    assert models.engine_installed("seaco-paraformer")


def test_helpers() -> None:
    assert compute_type("cpu") == "int8"
    assert compute_type("cuda") == "float16"
    assert compute_type("cpu", "float32") == "float32"
    assert strip_punct(" Hello,") == "Hello"
    assert strip_punct(' "don\'t."') == "don't"
    assert strip_punct(" ...") == ""


class _Word:
    def __init__(self, word: str, start: float, end: float) -> None:
        self.word, self.start, self.end = word, start, end


class _Seg:
    def __init__(self, text: str, words: list[_Word]) -> None:
        self.text, self.words = text, words


class FakeWhisperModel:
    instances: ClassVar[list[FakeWhisperModel]] = []

    def __init__(self, path: str, *, device: str, compute_type: str) -> None:
        self.path, self.device, self.compute_type = path, device, compute_type
        self.calls: list[dict[str, Any]] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[Any, None]:
        self.calls.append(kwargs)
        segs = [
            _Seg(
                " Hello, Kubernetes.", [_Word(" Hello,", 0.0, 0.4), _Word(" Kubernetes.", 0.5, 1.2)]
            ),
            _Seg(" Bye!", [_Word(" Bye!", 1.5, 1.8)]),
        ]
        return iter(segs), None


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    FakeWhisperModel.instances.clear()
    cache = tmp_path / "models"
    path = models.model_path(cache, WHISPER)
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(b"x")
    return cache


def test_whisper_asr_words_and_options(fake_whisper: Path) -> None:
    asr = WhisperAsr(fake_whisper, WHISPER, device="cpu")
    (model,) = FakeWhisperModel.instances
    assert model.compute_type == "int8" and model.path.endswith("faster-whisper-small")
    audio = np.zeros(16_000, dtype=np.float32)
    [(text, tokens)] = asr.transcribe_timed([audio], terms=["Kubernetes", "Grafana"])
    assert text == "Hello, Kubernetes. Bye!"
    assert tokens == [("Hello", 0.0, 0.4), ("Kubernetes", 0.5, 1.2), ("Bye", 1.5, 1.8)]
    call = model.calls[0]
    assert call["language"] == "en" and call["word_timestamps"] is True
    assert call["vad_filter"] is False and call["hotwords"] == "Kubernetes, Grafana"
    assert asr.transcribe([np.zeros(100, dtype=np.float32)]) == [""]  # too short: not decoded
    assert len(model.calls) == 1


def test_pipeline_with_whisper_engine(fake_whisper: Path, tmp_path: Path) -> None:
    asr = WhisperAsr(fake_whisper, WHISPER)
    engines = LocalEngines(vad=FakeVad(), asr=asr, aligner=None, diarizer=FakeDiarizer())
    provider = LocalPipelineProvider(cache_dir=fake_whisper, asr_model=WHISPER, engines=engines)
    audio = AudioSource(path=write_audio(tmp_path, "wav_s16", seconds=4.0))

    t = provider.transcribe(audio, JobOptions(provider="local", language_hints=["en"]), Recorder())
    assert t.warnings == []
    assert all(s.words for s in t.segments)
    texts = [s.text for s in t.segments]
    assert texts[:2] == ["Hello, Kubernetes.", "Bye!"]  # sentence ends split segments
    assert FakeWhisperModel.instances[0].calls[0]["language"] == "en"

    t = provider.transcribe(audio, JobOptions(provider="local"), Recorder())  # zh + en hints
    assert any("English-only route" in w for w in t.warnings)


async def test_job_route_needs_whisper_extra(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_models(env, ["fsmn-vad", WHISPER, "campplus"])
    install(app, FakeProvider())
    from server.routes import jobs as jobs_routes

    monkeypatch.setattr(jobs_routes.local_models, "local_stack_installed", lambda: True)
    monkeypatch.setattr(jobs_routes.local_models, "engine_installed", lambda key: False)
    upload = await add_upload(app, env)
    body = {
        "upload_id": upload.id,
        "provider": "local",
        "options": {"language_hints": ["en"], "local": {"asr_model": WHISPER}},
    }
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 422 and "`whisper` extra" in resp.json()["detail"]

    monkeypatch.setattr(jobs_routes.local_models, "engine_installed", lambda key: True)
    resp = await client.post("/api/jobs", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["options"]["local"]["asr_model"] == WHISPER


def test_latin_full_stop_splits_only_before_space() -> None:
    from asr.providers.local.merge import ChunkResult, Token, build_segments

    text = "Version 3.5 is out. Mr Smith agrees"
    words = ["Version", "3", "5", "is", "out", "Mr", "Smith", "agrees"]
    tokens = [Token(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)]
    segs = build_segments(
        [ChunkResult(start=0, end=3, text=text, tokens=tokens)], [], default_speaker="S"
    )
    assert [s.text for s in segs] == ["Version 3.5 is out.", "Mr Smith agrees"]


def test_compute_type_auto_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_LOCAL_DTYPE", "float32")
    assert compute_type("cpu", "auto") == "float32"
    monkeypatch.delenv("ASR_LOCAL_DTYPE")
    assert compute_type("cpu", "auto") == "int8"
    assert compute_type("cuda", "auto") == "float16"
