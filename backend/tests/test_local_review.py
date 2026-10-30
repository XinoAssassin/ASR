"""Local pipeline review regressions: cluster floor (m5), VAD cut points (m6), engine swap (n3),
lazy per-channel speaker ids (n5). Fakes only; no model is loaded."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from asr.providers.local.diar_campplus import fold_small_clusters
from asr.providers.local.vad import SAMPLE_RATE, merge_segments


# ---------------------------------------------------------------------------------------- m5
def _clusters(sizes: list[int], seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(len(sizes), 192))
    emb = np.vstack(
        [c + 0.1 * rng.normal(size=(n, 192)) for c, n in zip(centres, sizes, strict=True)]
    )
    labels = np.concatenate([np.full(n, k) for k, n in enumerate(sizes)])
    return emb, labels


def test_short_call_keeps_a_speaker_with_ten_seconds() -> None:
    # 3-minute call: ~240 windows; the third speaker talks ~10 s (13 windows, 5.4 %).
    emb, labels = _clusters([120, 107, 13])
    assert set(fold_small_clusters(emb, labels)) == {0, 1, 2}


def test_short_call_still_folds_a_blip() -> None:
    emb, labels = _clusters([120, 107, 4])  # 3 s of laughter / crosstalk
    out = fold_small_clusters(emb, labels)
    assert set(out) == {0, 1}


def test_long_recording_keeps_the_absolute_floor() -> None:
    # ~10 min (800 windows): 3 % = 24 windows; a 15-window cluster is folded.
    emb, labels = _clusters([400, 385, 15])
    assert set(fold_small_clusters(emb, labels)) == {0, 1}


# ---------------------------------------------------------------------------------------- m6
def _speech_with_pauses(seconds: float, pauses: list[float], seed: int = 0) -> np.ndarray:
    """Loud noise ("speech") with 120 ms near-silent gaps centred on ``pauses``."""
    rng = np.random.default_rng(seed)
    audio = (0.3 * rng.standard_normal(int(seconds * SAMPLE_RATE))).astype(np.float32)
    for p in pauses:
        a, b = int((p - 0.06) * SAMPLE_RATE), int((p + 0.06) * SAMPLE_RATE)
        audio[a:b] *= 0.001
    return audio


def test_long_segment_is_cut_in_pauses_not_mid_word() -> None:
    pauses = [19.1, 41.3]  # even cuts of a 60 s segment would be at 20 and 40
    audio = _speech_with_pauses(60.0, pauses)
    chunks = merge_segments([(0.0, 60.0)], max_len=25.0, audio=audio)
    assert len(chunks) == 3
    cuts = [e for _, e in chunks[:-1]]
    for cut, pause in zip(cuts, pauses, strict=True):
        assert abs(cut - pause) < 0.06
    assert chunks[0][0] == 0.0 and chunks[-1][1] == 60.0
    assert all(e - s <= 25.0 + 1e-9 for s, e in chunks)
    assert all(
        chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1)
    )  # no gap, no overlap


def test_cuts_never_exceed_max_len_even_when_the_pause_is_far() -> None:
    # 50 s segment, 2 pieces of 25 s: a cut 2 s early would leave a 27 s piece, so it stays put.
    audio = _speech_with_pauses(50.0, [23.0])
    chunks = merge_segments([(0.0, 50.0)], max_len=25.0, audio=audio)
    assert all(e - s <= 25.0 + 1e-9 for s, e in chunks)


def test_without_audio_the_split_is_even() -> None:
    chunks = merge_segments([(0.0, 60.0)], max_len=25.0)
    assert chunks == [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)]


def test_pipeline_passes_track_audio_to_the_chunker(tmp_path: Path, monkeypatch) -> None:
    from asr.providers.local import pipeline as pipeline_mod

    seen: list[np.ndarray | None] = []
    real = pipeline_mod.merge_segments

    def spy(segments, **kw):
        seen.append(kw.get("audio"))
        return real(segments, **kw)

    monkeypatch.setattr(pipeline_mod, "merge_segments", spy)
    _run_fake_pipeline(tmp_path, mono=True)
    assert seen and all(isinstance(a, np.ndarray) and len(a) > 0 for a in seen)


# ---------------------------------------------------------------------------------------- n3
def test_engine_swap_frees_the_old_set_before_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asr.providers.local.align as align_mod
    import asr.providers.local.asr_transformers as tr_mod
    import asr.providers.local.diar_campplus as diar_mod
    import asr.providers.local.vad as vad_mod
    from asr.providers.local import pipeline as pipeline_mod

    cache_sizes: list[int] = []

    class Fake:
        def __init__(self, *a, **k) -> None:
            cache_sizes.append(len(pipeline_mod._ENGINE_CACHE))

    for mod, name in [
        (tr_mod, "TransformersAsr"),
        (align_mod, "ForcedAligner"),
        (diar_mod, "CamPlusDiarizer"),
        (vad_mod, "FsmnVad"),
    ]:
        monkeypatch.setattr(mod, name, Fake)
    monkeypatch.setattr(pipeline_mod, "configure_threads", lambda: 1)
    monkeypatch.setattr(pipeline_mod, "_ENGINE_CACHE", {})
    first = pipeline_mod.load_engines(tmp_path, "qwen3-asr-0.6b", device="cpu", dtype="float32")
    assert (
        pipeline_mod.load_engines(tmp_path, "qwen3-asr-0.6b", device="cpu", dtype="float32")
        is first
    )
    cache_sizes.clear()
    pipeline_mod.load_engines(tmp_path, "qwen3-asr-1.7b", device="cpu", dtype="float32")
    assert cache_sizes and set(cache_sizes) == {0}  # the 0.6B set was released first
    assert len(pipeline_mod._ENGINE_CACHE) == 1


# ---------------------------------------------------------------------------------------- n5
class _Vad:
    def segments(self, audio):
        return [(0.0, len(audio) / SAMPLE_RATE)] if np.abs(audio).max() > 1e-3 else []


class _Asr:
    def transcribe(self, chunks, *, language=None, terms=(), context=None):
        return ["你好。" for _ in chunks]


class _Aligner:
    def align(self, audio, text, *, language="zh"):
        from asr.providers.local.align import TimedToken

        return [TimedToken("你", 0.1, 0.3), TimedToken("好", 0.3, 0.5)]


class _Diarizer:
    def __init__(self, labels: int = 2) -> None:
        self.labels = labels

    def diarize(self, audio, speech, *, num_speakers=None):
        from asr.providers.local.diar_campplus import Turn

        end = speech[-1][1] if speech else 0.0
        # Only label 0 ever overlaps the text; label 1 is a turn after it.
        return [Turn(0.0, 0.6, 0), Turn(0.6, end, 1)][: self.labels]


def _run_fake_pipeline(tmp_path: Path, *, mono: bool = False, silent_left: bool = False):
    from asr.providers.base import AudioSource, JobOptions
    from asr.providers.local.pipeline import LocalEngines, LocalPipelineProvider
    from tests.fixtures.dashscope.support import Recorder
    from tests.test_audio_fixtures import independent_stereo, write_audio

    if mono:
        path = write_audio(tmp_path, "wav_s16", seconds=2.0)
    else:
        samples = independent_stereo(2.0, 16_000).copy()
        if silent_left:
            samples[0] = 0
        path = write_audio(tmp_path, "wav_s16", samples=samples, name="a.wav")
    engines = LocalEngines(vad=_Vad(), asr=_Asr(), aligner=_Aligner(), diarizer=_Diarizer())
    provider = LocalPipelineProvider(cache_dir=tmp_path, engines=engines)
    options = JobOptions(provider="local")
    if not mono:
        options.preprocess.channels = "per_channel"
    return provider.transcribe(AudioSource(path=path), options, Recorder())


def test_silent_channel_zero_does_not_use_up_a_speaker_id(tmp_path: Path) -> None:
    t = _run_fake_pipeline(tmp_path, silent_left=True)
    assert {s.speaker_id for s in t.segments} == {"SPEAKER_00"}
    assert [s.id for s in t.speakers] == ["SPEAKER_00"]


def test_unused_diarization_label_does_not_use_up_a_speaker_id(tmp_path: Path) -> None:
    # ch 0 diarizes into two labels but only one of them speaks text; ch 1 is the next id.
    t = _run_fake_pipeline(tmp_path)
    assert sorted({s.speaker_id for s in t.segments}) == ["SPEAKER_00", "SPEAKER_01"]
    assert [s.id for s in t.speakers] == ["SPEAKER_00", "SPEAKER_01"]
