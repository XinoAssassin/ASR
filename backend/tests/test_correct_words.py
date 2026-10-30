"""[5b] corrections keep a segment's ``words`` consistent with its new text (review n6)."""

from __future__ import annotations

from asr.correct import correct_segments
from asr.editing import EditState
from asr.providers.base import CorrectionOptions, Segment, Term, Word
from asr.providers.local.merge import Token, token_spans


def timed_seg(text: str, words: list[str], step: float = 0.2) -> Segment:
    ws = [
        Word(text=w, start=round(i * step, 3), end=round(i * step + step, 3))
        for i, w in enumerate(words)
    ]
    return Segment(
        id="seg_00000", speaker_id="SPEAKER_00", start=0, end=len(words) * step, text=text, words=ws
    )


def tiles(seg: Segment) -> bool:
    return token_spans(seg.text, [Token(w.text, w.start, w.end) for w in seg.words]) is not None


def test_term_replacement_merges_the_words_it_covers() -> None:
    raw = timed_seg("汤泡真吓人", list("汤泡真吓人"))
    (out,), _ = correct_segments(
        [raw], [Term(term="Townfall", aliases=["汤泡"])], CorrectionOptions()
    )
    assert out.text == "Townfall真吓人"
    assert [(w.text, w.start, w.end) for w in out.words] == [
        ("Townfall", 0.0, 0.4),
        ("真", 0.4, 0.6),
        ("吓", 0.6, 0.8),
        ("人", 0.8, 1.0),
    ]
    assert tiles(out)


def test_collapsed_repetition_becomes_one_word() -> None:
    text = "我们我们我们我们我们我们一起"
    raw = timed_seg(text, list(text))
    (out,), _ = correct_segments([raw], [], CorrectionOptions())
    assert out.text == "我们一起"
    assert [w.text for w in out.words] == ["我们", "一", "起"]
    assert out.words[0].start == 0.0 and out.words[0].end == 2.4  # the whole collapsed run
    assert tiles(out)


def test_split_after_correction_uses_the_retiled_words() -> None:
    raw = timed_seg("汤泡真吓人", list("汤泡真吓人"))
    (out,), corrections = correct_segments(
        [raw], [Term(term="Townfall", aliases=["汤泡"])], CorrectionOptions()
    )
    state = EditState([out], [], corrections)
    state.split(out.id, len("Townfall"))
    left, right = state.segments
    assert (left.text, right.text) == ("Townfall", "真吓人")
    assert right.start == 0.4  # the word boundary, not a character-ratio guess
    assert [w.text for w in left.words] == ["Townfall"]


def test_unmappable_words_are_dropped_not_kept_stale() -> None:
    raw = timed_seg("汤泡真吓人", ["完全", "不同"])
    (out,), _ = correct_segments(
        [raw], [Term(term="Townfall", aliases=["汤泡"])], CorrectionOptions()
    )
    assert out.text == "Townfall真吓人"
    assert out.words == []


def test_unchanged_segment_keeps_its_words() -> None:
    raw = timed_seg("今天天气不错", list("今天天气不错"))
    (out,), corrections = correct_segments([raw], [], CorrectionOptions())
    assert corrections == [] and out is raw
