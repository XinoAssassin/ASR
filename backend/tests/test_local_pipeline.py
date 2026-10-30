"""Local pipeline (M5): merge logic, cluster folding, diarization metric, provider with fakes.

No model is loaded: VAD / ASR / aligner / diarizer are fakes; audio is real (decoded by PyAV).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from asr.eval.diarization import speaker_confusion
from asr.providers import get_provider
from asr.providers.base import AudioSource, JobCancelled, JobOptions, ProviderError, Term
from asr.providers.local import pipeline as pipeline_mod
from asr.providers.local.align import TimedToken
from asr.providers.local.diar_campplus import Turn, fold_small_clusters, order_labels
from asr.providers.local.merge import (
    ChunkResult,
    SpeakerTurn,
    Token,
    absorb_short_runs,
    build_segments,
    snap_changes,
    speaker_for,
    token_spans,
)
from asr.providers.local.pipeline import LocalEngines, LocalPipelineProvider, default_asr_model
from tests.fixtures.dashscope.support import KNOWN_STAGES, Recorder
from tests.test_audio_fixtures import independent_stereo, write_audio


def toks(text: str, start: float = 0.0, step: float = 0.2) -> list[Token]:
    """CJK chars / Latin words of ``text`` with evenly spaced timestamps."""
    from asr.eval.metrics import tokenize

    return [
        Token(t, start + i * step, start + (i + 1) * step - 0.02)
        for i, t in enumerate(tokenize(text))
    ]


# ------------------------------------------------------------------------------------- merge
def test_token_spans_case_and_width_insensitive() -> None:
    text = "我玩《寂静岭》，Ｔｏｗｎｆａｌｌ真好！"
    spans = token_spans(
        text, [Token(t, 0, 1) for t in ["我", "玩", "寂", "静", "岭", "townfall", "真", "好"]]
    )
    assert spans is not None
    assert text[spans[5][0] : spans[5][1]] == "Ｔｏｗｎｆａｌｌ"
    assert token_spans(text, [Token("你", 0, 1)]) is None


def test_speaker_for_overlap_and_nearest() -> None:
    turns = [SpeakerTurn(0, 2, "A"), SpeakerTurn(2, 4, "B")]
    assert speaker_for(1.5, 2.6, turns) == "B"
    assert speaker_for(5.0, 5.2, turns) == "B"  # nothing overlaps: nearest turn
    assert speaker_for(0, 1, []) is None


def test_absorb_short_runs() -> None:
    tokens = toks("一二三四五六七八")
    labels = ["A", "A", "A", "B", "A", "A", "A", "A"]
    assert absorb_short_runs(labels, tokens) == ["A"] * 8
    labels = ["A", "A", "A", "B", "B", "B", "B", "B"]
    assert absorb_short_runs(labels, tokens) == labels  # long enough to stay


def test_snap_changes_prefers_punctuation() -> None:
    text = "我们先聊这个，然后你说吧"
    tokens = toks(text)
    spans = token_spans(text, tokens)
    assert spans is not None
    # Diarization switches two characters after the comma; snap back to the comma.
    labels = ["A"] * 8 + ["B"] * 3
    assert len(labels) == len(tokens)
    out = snap_changes(labels, tokens, text, spans)
    assert out[:6] == ["A"] * 6 and out[6:] == ["B"] * 5


def test_snap_changes_uses_pause_without_punctuation() -> None:
    tokens = [Token(c, i * 0.2, i * 0.2 + 0.18) for i, c in enumerate("一二三四")]
    tokens += [Token(c, 1.5 + i * 0.2, 1.5 + i * 0.2 + 0.18) for i, c in enumerate("五六七八")]
    text = "一二三四五六七八"
    spans = token_spans(text, tokens)
    assert spans is not None
    labels = ["A"] * 6 + ["B"] * 2
    assert snap_changes(labels, tokens, text, spans) == ["A"] * 4 + ["B"] * 4


def test_build_segments_sentences_speakers_and_words() -> None:
    text = "大家好，我是主持人。今天聊聊Townfall！"
    tokens = toks(text, start=10.0)
    split = 6 * 0.2 + 10.0  # after "大家好我是主"
    chunk = ChunkResult(start=10.0, end=10.0 + len(tokens) * 0.2, text=text, tokens=tokens)
    turns = [
        SpeakerTurn(10.0, split + 0.5, "SPEAKER_00"),
        SpeakerTurn(split + 0.5, 20.0, "SPEAKER_01"),
    ]
    segs = build_segments([chunk], turns, default_speaker="SPEAKER_00")
    assert [(s.speaker_id, s.text) for s in segs] == [
        ("SPEAKER_00", "大家好，我是主持人。"),
        ("SPEAKER_01", "今天聊聊Townfall！"),
    ]
    assert segs[0].start == 10.0 and segs[1].words[-1].text == "townfall"
    assert all(s.start <= s.end for s in segs)


def test_build_segments_pause_split_and_fallbacks() -> None:
    tokens = [Token("一", 0.0, 0.2), Token("二", 0.3, 0.5), Token("三", 2.0, 2.2)]
    chunk = ChunkResult(0.0, 2.5, "一二三", tokens)
    segs = build_segments([chunk], [], default_speaker="SPEAKER_05")
    assert [s.text for s in segs] == ["一二", "三"]  # 1.5 s pause splits
    assert {s.speaker_id for s in segs} == {"SPEAKER_05"}
    # No tokens (alignment failed): one segment for the chunk, speaker by overlap.
    bare = ChunkResult(3.0, 5.0, "没有时间戳", [])
    segs = build_segments([bare], [SpeakerTurn(2.5, 6.0, "SPEAKER_01")], default_speaker="X")
    assert [(s.speaker_id, s.start, s.end) for s in segs] == [("SPEAKER_01", 3.0, 5.0)]
    # Fixed speaker overrides turns.
    fixed = ChunkResult(0.0, 1.0, "你好", toks("你好"), speaker="SPEAKER_09")
    segs = build_segments([fixed], [SpeakerTurn(0, 1, "SPEAKER_01")], default_speaker="X")
    assert segs[0].speaker_id == "SPEAKER_09"


# ---------------------------------------------------------------------------- diarization
def test_fold_small_clusters() -> None:
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=192), rng.normal(size=192)
    emb = np.vstack(
        [
            a + 0.1 * rng.normal(size=(100, 192)),
            b + 0.1 * rng.normal(size=(100, 192)),
            a + 0.1 * rng.normal(size=(3, 192)),
        ]
    )
    labels = np.array([0] * 100 + [1] * 100 + [2] * 3)
    out = fold_small_clusters(emb, labels)
    assert set(out) == {0, 1}
    assert (out[-3:] == 0).all()  # the tiny cluster sits next to speaker 0


def test_order_labels_by_first_appearance() -> None:
    turns = order_labels([Turn(5, 6, 3), Turn(0, 1, 7), Turn(2, 3, 3)])
    assert [(t.start, t.label) for t in turns] == [(0, 0), (2, 1), (5, 1)]


def test_speaker_confusion() -> None:
    ref = [(0, 10, "x"), (10, 20, "y")]
    hyp = [(0, 9, 1), (9, 20, 2)]  # one second of x labelled as the y-speaker
    c = speaker_confusion(ref, hyp)
    assert c.mapping == {1: "x", 2: "y"}
    assert c.rate == pytest.approx(0.05, abs=0.002)
    assert speaker_confusion(ref, [(0, 20, "only")]).rate == pytest.approx(0.5, abs=0.002)
    assert speaker_confusion([], hyp).rate == 0.0


# ------------------------------------------------------------------------- provider (fakes)
class FakeVad:
    def segments(self, audio: np.ndarray) -> list[tuple[float, float]]:
        dur = len(audio) / 16_000
        return [(0.1, dur / 2 - 0.1), (dur / 2 + 0.1, dur - 0.1)]


class FakeAsr:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, chunks, *, language=None, terms=(), context=None):
        self.calls.append({"language": language, "terms": list(terms), "context": context})
        return ["大家好，我们开始。" for _ in chunks]


class FakeAligner:
    def align(self, audio: np.ndarray, text: str, *, language: str = "zh") -> list[TimedToken]:
        from asr.eval.metrics import tokenize

        words = tokenize(text)
        step = len(audio) / 16_000 / max(len(words), 1)
        return [TimedToken(w, i * step, (i + 1) * step - 0.01) for i, w in enumerate(words)]


class FakeDiarizer:
    def __init__(self) -> None:
        self.calls: list[int | None] = []

    def diarize(self, audio, speech, *, num_speakers):
        self.calls.append(num_speakers)
        dur = len(audio) / 16_000
        return [Turn(0.0, dur / 2, 0), Turn(dur / 2, dur, 1)]


def fake_engines() -> LocalEngines:
    return LocalEngines(
        vad=FakeVad(), asr=FakeAsr(), aligner=FakeAligner(), diarizer=FakeDiarizer()
    )


def local_provider(tmp_path: Path, engines: LocalEngines | None = None) -> LocalPipelineProvider:
    return LocalPipelineProvider(cache_dir=tmp_path / "models", engines=engines or fake_engines())


def test_mono_pipeline(tmp_path: Path) -> None:
    engines = fake_engines()
    audio = write_audio(tmp_path, "wav_s16", seconds=4.0)
    rec = Recorder()
    t = local_provider(tmp_path, engines).transcribe(
        AudioSource(path=audio),
        JobOptions(provider="local", terms=[Term(term="Townfall")], context="游戏播客"),
        rec,
    )
    assert t.provider == "local" and t.duration == pytest.approx(4.0, abs=0.05)
    assert [s.id for s in t.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert {s.speaker_id for s in t.segments} == {"SPEAKER_00", "SPEAKER_01"}
    assert all(s.words for s in t.segments)
    assert engines.asr.calls[0] == {"language": "zh", "terms": ["Townfall"], "context": "游戏播客"}
    assert engines.diarizer.calls == [None]
    assert {e.stage for e in rec.events} <= KNOWN_STAGES
    assert {"decoding", "vad", "asr", "aligning", "diarizing", "merging"} <= {
        e.stage for e in rec.events
    }
    values = [e.progress for e in rec.events]
    assert values == sorted(values) and values[-1] == 1.0


def test_per_channel_diarizes_channel_zero_only(tmp_path: Path) -> None:
    engines = fake_engines()
    audio = write_audio(
        tmp_path, "wav_s16", samples=independent_stereo(4.0, 16_000), name="call.wav"
    )
    options = JobOptions(provider="local")
    options.preprocess.channels = "per_channel"
    t = local_provider(tmp_path, engines).transcribe(AudioSource(path=audio), options, Recorder())
    # Channel 0 -> two diarized speakers; channel 1 -> one fixed speaker after them.
    assert {s.id for s in t.speakers} == {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"}
    channel1 = {s.speaker_id for s in t.segments if s.speaker_id == "SPEAKER_02"}
    assert channel1 == {"SPEAKER_02"}
    assert engines.diarizer.calls == [None]
    starts = [s.start for s in t.segments]
    assert starts == sorted(starts)
    assert [s.id for s in t.segments] == [f"seg_{i:05d}" for i in range(len(t.segments))]


def test_single_speaker_skips_diarization(tmp_path: Path) -> None:
    engines = fake_engines()
    audio = write_audio(tmp_path, "wav_s16", seconds=3.0)
    t = local_provider(tmp_path, engines).transcribe(
        AudioSource(path=audio), JobOptions(provider="local", num_speakers=1), Recorder()
    )
    assert engines.diarizer.calls == []
    assert [s.id for s in t.speakers] == ["SPEAKER_00"]


def test_paraformer_style_engine_skips_aligner(tmp_path: Path) -> None:
    class TimedAsr(FakeAsr):
        def transcribe_timed(self, chunks, *, terms=(), language=None):
            return [
                (
                    "你好世界",
                    [("你", 0.0, 0.2), ("好", 0.2, 0.4), ("世", 0.5, 0.7), ("界", 0.7, 0.9)],
                )
                for _ in chunks
            ]

    engines = LocalEngines(vad=FakeVad(), asr=TimedAsr(), aligner=None, diarizer=FakeDiarizer())
    t = local_provider(tmp_path, engines).transcribe(
        AudioSource(path=write_audio(tmp_path, "wav_s16", seconds=3.0)),
        JobOptions(provider="local"),
        Recorder(),
    )
    assert engines.asr.calls == []  # only transcribe_timed was used
    assert all(len(s.words) >= 1 for s in t.segments)


def test_cancellation_and_url_source(tmp_path: Path) -> None:
    audio = write_audio(tmp_path, "wav_s16", seconds=3.0)
    for stage in ("decoding", "asr", "diarizing"):
        with pytest.raises(JobCancelled):
            local_provider(tmp_path).transcribe(
                AudioSource(path=audio), JobOptions(provider="local"), Recorder(cancel_on=stage)
            )
    with pytest.raises(ProviderError, match="uploaded file"):
        local_provider(tmp_path).transcribe(
            AudioSource(url="https://example.com/a.mp3"), JobOptions(provider="local"), Recorder()
        )


def test_missing_models_raise_provider_error(tmp_path: Path) -> None:
    pipeline_mod._ENGINE_CACHE.clear()
    provider = get_provider("local", cache_dir=tmp_path / "empty", asr_model="qwen3-asr-0.6b")
    with pytest.raises(ProviderError, match="asr models download"):
        provider.transcribe(
            AudioSource(path=write_audio(tmp_path, "wav_s16", seconds=1.0)),
            JobOptions(provider="local"),
            Recorder(),
        )
    with pytest.raises(ProviderError, match="not an ASR model"):
        get_provider("local", cache_dir=tmp_path, asr_model="campplus").transcribe(
            AudioSource(path=write_audio(tmp_path, "wav_s16", seconds=1.0)),
            JobOptions(provider="local"),
            Recorder(),
        )


def test_default_asr_model_follows_bf16(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_mod, "resolve_dtype", lambda *a, **k: "bfloat16")
    assert default_asr_model() == "qwen3-asr-1.7b"
    monkeypatch.setattr(pipeline_mod, "resolve_dtype", lambda *a, **k: "float32")
    assert default_asr_model() == "qwen3-asr-0.6b"


def test_zero_length_token_gets_minimum_duration() -> None:
    tokens = [Token("啊", 1.0, 1.0)]
    segs = build_segments([ChunkResult(0.9, 2.0, "啊，", tokens)], [], default_speaker="S")
    assert segs[0].start == 1.0 and segs[0].end == pytest.approx(1.08)
    # Never past the chunk end.
    segs = build_segments([ChunkResult(0.9, 1.03, "啊", tokens)], [], default_speaker="S")
    assert segs[0].end == pytest.approx(1.03)


# ------------------------------------------------------------------- review regressions (M12)
ALIGNER_DROPS = [
    ("我们用GPT-4写代码", "GPT-4", "GPT4"),
    ("升级到3.5版本", "3.5", "35"),
    ("做real-time转写", "real-time", "realtime"),
]


@pytest.mark.parametrize(("text", "word", "token"), ALIGNER_DROPS)
def test_token_spans_cover_characters_the_aligner_drops(text: str, word: str, token: str) -> None:
    from asr.eval.metrics import tokenize

    words = [token if w == word else w for w in tokenize(text)]
    try:  # the real aligner tokenizer, when transformers is installed
        from transformers.models.qwen3_asr.processing_qwen3_asr import Qwen3ASRProcessor

        words = Qwen3ASRProcessor.split_words_for_alignment(None, text)
    except ImportError:
        pass
    assert token in words
    spans = token_spans(text, [Token(w, 0, 1) for w in words])
    assert spans is not None
    assert text[spans[words.index(token)][0] : spans[words.index(token)][1]] == word
    assert "".join(text[a:b] for a, b in spans) == text  # spans tile the text (no punctuation)


def test_token_spans_never_skip_whitespace_or_kept_characters() -> None:
    assert token_spans("GPT 4", [Token("GPT4", 0, 1)]) is None
    assert token_spans("GPTx4", [Token("GPT4", 0, 1)]) is None
    spans = token_spans("C++ 和 e-mail, OK.", [Token(t, 0, 1) for t in ["C", "和", "email", "OK"]])
    assert spans is not None and spans[2] == (6, 12) and spans[3] == (14, 16)


def test_build_segments_keeps_speakers_and_words_with_dropped_punctuation() -> None:
    text = "我们用GPT-4写代码。你说3.5呢？"
    words = ["我", "们", "用", "GPT4", "写", "代", "码", "你", "说", "35", "呢"]
    tokens = [Token(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)]
    chunk = ChunkResult(0.0, len(words) * 0.3, text, tokens)
    turns = [SpeakerTurn(0.0, 2.1, "SPEAKER_00"), SpeakerTurn(2.1, 10.0, "SPEAKER_01")]
    segs = build_segments([chunk], turns, default_speaker="SPEAKER_00")
    assert [(s.speaker_id, s.text) for s in segs] == [
        ("SPEAKER_00", "我们用GPT-4写代码。"),
        ("SPEAKER_01", "你说3.5呢？"),
    ]
    assert [len(s.words) for s in segs] == [7, 4]


def test_snap_changes_does_not_erase_a_short_run() -> None:
    text = "一二三四五六，七八九"
    tokens = [Token(c, i * 0.2, i * 0.2 + 0.18) for i, c in enumerate("一二三四五六七八九")]
    spans = token_spans(text, tokens)
    assert spans is not None
    out = snap_changes(list("AAABBCCCC"), tokens, text, spans)
    assert out == list("AAABBBCCC")  # B -> C snaps to the comma; B survives


def test_fold_small_clusters_reassigns_hdbscan_outliers() -> None:
    from asr.providers.local.diar_campplus import reassign_outliers

    rng = np.random.default_rng(1)
    a, b = rng.normal(size=192), rng.normal(size=192)
    emb = np.vstack(
        [
            a + 0.1 * rng.normal(size=(100, 192)),
            b + 0.1 * rng.normal(size=(100, 192)),
            b + 0.1 * rng.normal(size=(50, 192)),  # HDBSCAN noise, but 20 % of the windows
        ]
    )
    labels = np.array([0] * 100 + [1] * 100 + [-1] * 50)
    out = fold_small_clusters(emb, labels)
    assert set(out) == {0, 1} and (out[-50:] == 1).all()
    out = reassign_outliers(emb, labels)
    assert set(out) == {0, 1} and (out[-50:] == 1).all()
    assert set(fold_small_clusters(emb, np.full(250, -1))) == {0}


def test_per_channel_oracle_excludes_fixed_channels(tmp_path: Path) -> None:
    audio = write_audio(
        tmp_path, "wav_s16", samples=independent_stereo(4.0, 16_000), name="call.wav"
    )
    for total, calls, speakers in ((3, [2], 3), (2, [], 2)):
        engines = fake_engines()
        options = JobOptions(provider="local", num_speakers=total)
        options.preprocess.channels = "per_channel"
        t = local_provider(tmp_path, engines).transcribe(
            AudioSource(path=audio), options, Recorder()
        )
        assert engines.diarizer.calls == calls
        assert len(t.speakers) == speakers


def test_stored_probe_of_another_stream_is_reprobed(tmp_path: Path) -> None:
    from asr.audio import decode
    from tests.test_audio_fixtures import write_multistream

    path = write_multistream(tmp_path)  # stream 0 mono, stream 1 stereo
    stale = decode.probe(path, 0)
    options = JobOptions(provider="local")
    options.preprocess.stream_index = 1
    options.preprocess.channels = "per_channel"  # needs stream 1's two channels
    engines = fake_engines()
    t = local_provider(tmp_path, engines).transcribe(
        AudioSource(path=path, probe=stale), options, Recorder()
    )
    assert len(t.speakers) == 3  # channel 0 diarized into two, channel 1 fixed


def test_language_hints_are_normalised() -> None:
    from asr.providers.local.asr_transformers import engine_language
    from asr.providers.local.pipeline import _language

    assert _language(["zh-CN", "en"]) == "zh"
    assert _language(["zh_Hans"]) == "zh"
    assert _language(["en-US"]) == "en"
    assert _language(["auto"]) is None
    assert _language(["xx", "ja"]) == "ja"
    assert _language(["klingon"]) is None
    assert _language([]) is None
    assert engine_language("qwen3_asr", "ko") == "ko"
    assert engine_language("fun_asr_nano", "ko") is None
    assert engine_language("qwen3_asr", None) is None


@pytest.mark.parametrize("error", [ImportError("needs nagisa"), ValueError("unsupported")])
def test_aligner_failure_falls_back_with_warning(tmp_path: Path, error: Exception) -> None:
    class FailingAligner:
        def align(self, audio, text, *, language="zh"):
            raise error

    engines = LocalEngines(
        vad=FakeVad(), asr=FakeAsr(), aligner=FailingAligner(), diarizer=FakeDiarizer()
    )
    options = JobOptions(provider="local", language_hints=["ja-JP"])
    t = local_provider(tmp_path, engines).transcribe(
        AudioSource(path=write_audio(tmp_path, "wav_s16", seconds=3.0)), options, Recorder()
    )
    assert engines.asr.calls[0]["language"] == "ja"
    assert t.segments and all(not s.words for s in t.segments)
    assert sum(w.startswith("Word timestamps unavailable") for w in t.warnings) == 1


def test_default_asr_model_reads_dtype_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from asr.providers.local import device

    monkeypatch.setattr(device, "cpu_features", lambda: device.CpuFeatures("AVX2", False))
    monkeypatch.setenv("ASR_LOCAL_DTYPE", "bfloat16")
    assert default_asr_model() == "qwen3-asr-1.7b"
    assert LocalPipelineProvider(cache_dir=Path("x")).asr_model == "qwen3-asr-1.7b"
    monkeypatch.setenv("ASR_LOCAL_DTYPE", "float32")
    monkeypatch.setattr(device, "cpu_features", lambda: device.CpuFeatures("AVX512", True))
    assert default_asr_model() == "qwen3-asr-0.6b"
