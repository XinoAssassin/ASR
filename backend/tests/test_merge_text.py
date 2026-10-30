"""Merge never loses ASR text (review M1) and folds text and tokens alike (review n1).

Tokens are produced the way Qwen3-ForcedAligner produces them (the real
``split_words_for_alignment`` when transformers is installed, a faithful copy otherwise), so the
characters it drops (``《 》 + $ ℃`` emoji ...) sit between, before and after the tokens.
"""

from __future__ import annotations

import itertools
import random

import pytest

from asr.providers.local.merge import (
    ChunkResult,
    SpeakerTurn,
    Token,
    _aligner_kept,
    _is_cjk,
    build_segments,
    token_spans,
)


def aligner_words(text: str) -> list[str]:
    try:
        from transformers.models.qwen3_asr.processing_qwen3_asr import Qwen3ASRProcessor

        return Qwen3ASRProcessor.split_words_for_alignment(None, text)
    except ImportError:  # same rule: CJK chars alone, kept chars of space-separated words
        words: list[str] = []
        buf: list[str] = []
        for ch in text.strip():
            if _is_cjk(ch) or ch.isspace():
                if buf:
                    words.append("".join(buf))
                    buf.clear()
                if _is_cjk(ch):
                    words.append(ch)
            elif _aligner_kept(ch):
                buf.append(ch)
        if buf:
            words.append("".join(buf))
        return words


def timed(text: str, start: float = 0.0, step: float = 0.3) -> list[Token]:
    return [
        Token(w, start + i * step, start + i * step + step - 0.05)
        for i, w in enumerate(aligner_words(text))
    ]


def squash(text: str) -> str:
    return "".join(text.split())


def joined(segments) -> str:
    return squash("".join(s.text for s in segments))


# --------------------------------------------------------------------------- M1 regressions
def test_text_before_the_first_token_is_kept() -> None:
    text = "《三体》很好看"
    segs = build_segments([ChunkResult(0, 3, text, timed(text))], [], default_speaker="S")
    assert [s.text for s in segs] == ["《三体》很好看"]


def test_symbols_at_a_speaker_change_are_kept_and_end_the_sentence() -> None:
    text = "我用C++。你呢"
    tokens = timed(text)  # 我 用 C 你 呢
    assert [t.text for t in tokens] == ["我", "用", "C", "你", "呢"]
    turns = [SpeakerTurn(0.0, 0.9, "A"), SpeakerTurn(0.9, 9.0, "B")]
    segs = build_segments([ChunkResult(0, 2, text, tokens)], turns, default_speaker="A")
    assert [(s.speaker_id, s.text) for s in segs] == [("A", "我用C++。"), ("B", "你呢")]


def test_sentence_end_after_a_symbol_splits() -> None:
    text = "今天30℃。好热"
    segs = build_segments([ChunkResult(0, 3, text, timed(text))], [], default_speaker="S")
    assert [s.text for s in segs] == ["今天30℃。", "好热"]


def test_leading_currency_stays_with_its_number() -> None:
    text = "好的。$100可以"
    segs = build_segments([ChunkResult(0, 3, text, timed(text))], [], default_speaker="S")
    assert [s.text for s in segs] == ["好的。", "$100可以"]


def test_opening_quote_goes_to_the_next_segment() -> None:
    text = "他走了。“你呢？”"
    segs = build_segments([ChunkResult(0, 3, text, timed(text))], [], default_speaker="S")
    assert [s.text for s in segs] == ["他走了。", "“你呢？”"]


def test_trailing_text_after_the_last_sentence_end_is_kept() -> None:
    text = "你好。😀"
    segs = build_segments([ChunkResult(0, 3, text, timed(text))], [], default_speaker="S")
    assert joined(segs) == squash(text)
    assert segs[-1].text.endswith("😀")


def test_emoji_and_symbols_at_a_pause_are_kept() -> None:
    text = "好的😀走吧"
    tokens = timed(text)
    tokens = tokens[:2] + [Token(t.text, t.start + 2.0, t.end + 2.0) for t in tokens[2:]]
    segs = build_segments([ChunkResult(0, 5, text, tokens)], [], default_speaker="S")
    assert [s.text for s in segs] == ["好的😀", "走吧"]


TRICKY = [
    "《三体》很好看",
    "我用C++。你呢",
    "今天30℃。好热",
    "好的。$100可以，¥50也行",
    "他说：“你好。”然后走了",
    "版权©2024，商标™。",
    "x²+y²=z²？对的",
    "好的😀走吧👍",
    "价格是€5~€10。OK?",
    "<b>加粗</b>的文字",
    "C# and F# are fine. Ruby++ too!",
    "  前后有空格  ",
    "ID: #42, 进度 50%…完成！",
    "【公告】（重要）：请于3/4前提交——谢谢",
    "e-mail me @ foo_bar ^_^ ok",
    "½杯水，ﬁle系统，İstanbul。",
    "Ⅻ点钟，㎏单位。",
    "...开头是省略号",
    "末尾是符号+++",
    "A→B→C，完成✓",
]


@pytest.mark.parametrize("text", TRICKY)
def test_segment_texts_concatenate_to_the_chunk_text(text: str) -> None:
    """Property: whatever the speakers, pauses and sentence ends, no character is lost or
    duplicated (whitespace at the cuts aside)."""
    tokens = timed(text, start=5.0)
    assert token_spans(text, tokens) is not None, "tokens must map onto the text"
    rng = random.Random(text)
    end = tokens[-1].end if tokens else 6.0
    for _ in range(40):
        # Random speaker turns and random pauses between tokens.
        shifted, offset = [], 0.0
        for t in tokens:
            if rng.random() < 0.2:
                offset += 1.0
            shifted.append(Token(t.text, t.start + offset, t.end + offset))
        cuts = sorted(rng.uniform(5.0, end + offset) for _ in range(rng.randint(0, 4)))
        bounds = [5.0, *cuts, end + offset + 1.0]
        turns = [SpeakerTurn(a, b, f"S{rng.randint(0, 2)}") for a, b in itertools.pairwise(bounds)]
        chunk = ChunkResult(5.0, end + offset + 1.0, text, shifted)
        segs = build_segments([chunk], turns, default_speaker="S0")
        assert joined(segs) == squash(text)
        assert all(s.text == s.text.strip() and s.text for s in segs)


# --------------------------------------------------------------------------------------- n1
@pytest.mark.parametrize(
    ("text", "word"), [("我喝了½杯", "½"), ("打开ﬁle", "ﬁle"), ("去İstanbul玩", "İstanbul")]
)
def test_compatibility_characters_match_their_tokens(text: str, word: str) -> None:
    tokens = timed(text)
    assert word in [t.text for t in tokens]
    spans = token_spans(text, tokens)
    assert spans is not None
    k = [t.text for t in tokens].index(word)
    assert text[spans[k][0] : spans[k][1]] == word


def test_compatibility_character_keeps_per_token_speakers() -> None:
    text = "我喝了½杯。你呢"
    tokens = timed(text)
    turns = [SpeakerTurn(0.0, 1.5, "A"), SpeakerTurn(1.5, 9.0, "B")]
    segs = build_segments([ChunkResult(0, 3, text, tokens)], turns, default_speaker="A")
    assert [(s.speaker_id, s.text) for s in segs] == [("A", "我喝了½杯。"), ("B", "你呢")]
