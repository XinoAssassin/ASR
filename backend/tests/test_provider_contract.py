"""Reusable provider contract (AGENTS.md rule 4). Every provider must pass it.

Add a provider by adding a ``ProviderCase`` to ``CASES``: ``build`` returns a ready provider
(or raises ``ProviderError`` when the provider is not available yet), ``source`` the input,
and ``cancel_stage`` a stage at which raising ``JobCancelled`` must abort the run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import respx

from asr.providers.base import (
    AudioSource,
    JobCancelled,
    JobOptions,
    ProviderError,
    Term,
    Transcript,
    TranscriptionProvider,
)
from tests.fixtures.dashscope.support import (
    KNOWN_STAGES,
    Recorder,
    fake_probe,
    make_audio,
    make_provider,
    mock_dashscope,
    patch_audio,
)


@dataclass
class ProviderCase:
    name: str
    available: bool
    # (tmp_path, monkeypatch) -> context manager yielding (provider, source)
    setup: Callable[[Path, pytest.MonkeyPatch], object]
    cancel_stage: str | None = None
    first_stage: str = "preparing"


@contextmanager
def _cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    patch_audio(monkeypatch, probe=fake_probe())
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        mock_dashscope(router)
        work = tmp_path / "work"
        work.mkdir()
        yield make_provider(work), AudioSource(path=make_audio(tmp_path / "in"))


@contextmanager
def _local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    from asr.providers.local.pipeline import LocalPipelineProvider
    from tests.test_audio_fixtures import write_audio
    from tests.test_local_pipeline import fake_engines

    provider = LocalPipelineProvider(cache_dir=tmp_path / "models", engines=fake_engines())
    (tmp_path / "in").mkdir()
    yield provider, AudioSource(path=write_audio(tmp_path / "in", "wav_s16", seconds=4.0))


CASES = [
    ProviderCase("cloud", available=True, setup=_cloud, cancel_stage="recognizing"),
    ProviderCase("local", available=True, setup=_local, cancel_stage="asr", first_stage="decoding"),
]

OPTIONS = JobOptions(num_speakers=4, terms=[Term(term="Kubernetes", weight=5)])


@pytest.fixture(params=CASES, ids=lambda c: c.name)
def case(request) -> ProviderCase:
    return request.param


def _run(case: ProviderCase, tmp_path, monkeypatch, recorder: Recorder) -> Transcript:
    with case.setup(tmp_path, monkeypatch) as (provider, source):  # type: ignore[attr-defined]
        assert isinstance(provider, TranscriptionProvider)
        assert provider.name == case.name
        return provider.transcribe(source, OPTIONS, recorder)


def test_unavailable_provider_raises_provider_error(case, tmp_path, monkeypatch):
    if case.available:
        pytest.skip("provider is available")
    with pytest.raises(ProviderError):
        _run(case, tmp_path, monkeypatch, Recorder())


def test_returns_valid_transcript(case, tmp_path, monkeypatch):
    if not case.available:
        pytest.skip("provider not available yet")
    recorder = Recorder()
    t = _run(case, tmp_path, monkeypatch, recorder)

    assert isinstance(t, Transcript)
    assert t.provider == case.name
    assert t.segments, "a non-empty recording must yield segments"
    assert t.duration is not None and t.duration > 0

    # Seconds, sorted, inside the recording.
    starts = [s.start for s in t.segments]
    assert starts == sorted(starts)
    for s in t.segments:
        assert 0 <= s.start <= s.end <= t.duration
        for w in s.words:
            assert 0 <= w.start <= w.end <= t.duration
    assert t.duration < 24 * 3600, "duration looks like milliseconds"

    # Ids: unique segments, every speaker known, opaque SPEAKER_nn ids.
    ids = [s.id for s in t.segments]
    assert len(ids) == len(set(ids))
    speaker_ids = {sp.id for sp in t.speakers}
    assert {s.speaker_id for s in t.segments} <= speaker_ids
    assert len(speaker_ids) == len(t.speakers)
    assert all(sp.id.startswith("SPEAKER_") for sp in t.speakers)

    # No storage / provider-specific fields.
    dumped = t.model_dump()
    assert set(dumped) == {"provider", "duration", "language", "speakers", "segments", "warnings"}
    assert "original" not in dumped
    assert all(not s.edited for s in t.segments)

    # Progress: known stage keys, monotonic, within [0, 1].
    assert recorder.events
    assert {e.stage for e in recorder.events} <= KNOWN_STAGES
    values = [e.progress for e in recorder.events]
    assert values == sorted(values)
    assert all(0.0 <= v <= 1.0 for v in values)


def test_job_cancelled_propagates(case, tmp_path, monkeypatch):
    if not case.available:
        pytest.skip("provider not available yet")
    assert case.cancel_stage is not None
    with pytest.raises(JobCancelled):
        _run(case, tmp_path, monkeypatch, Recorder(cancel_on=case.cancel_stage))


def test_job_cancelled_on_first_progress(case, tmp_path, monkeypatch):
    if not case.available:
        pytest.skip("provider not available yet")
    with pytest.raises(JobCancelled):
        _run(case, tmp_path, monkeypatch, Recorder(cancel_on=case.first_stage))
