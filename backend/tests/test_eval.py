"""Bake-off helpers: subtitles, offset estimation, mixed error rate, term recall, chunking."""

from __future__ import annotations

from pathlib import Path

import pytest

from asr.eval.metrics import edit_counts, mixed_error_rate, term_recall, tokenize
from asr.eval.subtitles import (
    Cue,
    estimate_offset,
    load_subtitles,
    parse_ass,
    parse_srt,
    parse_vtt,
    select_window,
)
from asr.providers.local.vad import merge_segments

ASS = """[Script Info]
Title: x

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\i1}第一句,带逗号{\\i0}
Dialogue: 0,0:00:00.20,0:00:01.00,Default,,0,0,0,,开头\\N第二行
Comment: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,not a cue
"""


def test_parse_ass_keeps_commas_and_strips_tags() -> None:
    cues = parse_ass(ASS)
    assert cues == [Cue(0.2, 1.0, "开头 第二行"), Cue(1.5, 3.0, "第一句,带逗号")]


def test_parse_srt_and_vtt() -> None:
    srt = (
        "1\n00:00:01,000 --> 00:00:02,500\n<i>你好</i>\nworld\n\n"
        "2\n00:01:00,5 --> 00:01:01,000\n再见\n"
    )
    assert parse_srt(srt) == [Cue(1.0, 2.5, "你好 world"), Cue(60.5, 61.0, "再见")]
    vtt = (
        "WEBVTT\n\n00:01.000 --> 00:02.000\n<v A>甲</v>\n\nid\n01:00:00.000 --> 01:00:01.250\n乙\n"
    )
    assert parse_vtt(vtt) == [Cue(1.0, 2.0, "甲"), Cue(3600.0, 3601.25, "乙")]


def test_load_subtitles_by_suffix(tmp_path: Path) -> None:
    (tmp_path / "a.ass").write_text(ASS, encoding="utf-8")
    assert len(load_subtitles(tmp_path / "a.ass")) == 2
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        load_subtitles(tmp_path / "a.txt")


def test_select_window_pads_without_entering_neighbours() -> None:
    cues = [Cue(0, 1, "a"), Cue(1.2, 2, "b"), Cue(5, 6, "c"), Cue(6.1, 7, "d")]
    inside, lo, hi = select_window(cues, 1.0, 6.5)
    assert [c.text for c in inside] == ["b", "c"]
    assert lo == pytest.approx(1.1)  # midpoint to the previous cue, not 1.2 - 0.4
    assert hi == pytest.approx(6.05)
    with pytest.raises(ValueError):
        select_window(cues, 2.1, 4.9)


def test_estimate_offset_recovers_shift() -> None:
    speech = [(10.0, 12.0), (15.0, 18.5), (30.0, 31.0), (40.0, 44.0)]
    cues = [Cue(s - 2.3, e - 2.3, "x") for s, e in speech]
    offset, overlap = estimate_offset(speech, cues)
    assert offset == pytest.approx(2.3, abs=0.06)
    assert overlap > 0.95


def test_tokenize_mixed_text() -> None:
    assert tokenize("我玩《寂静岭：Townfall》，Jump Scare！２D") == [
        "我",
        "玩",
        "寂",
        "静",
        "岭",
        "townfall",
        "jump",
        "scare",
        "2d",
    ]


def test_edit_counts() -> None:
    c = edit_counts(list("abcdef"), list("abxdefg"))
    assert (c.substitutions, c.deletions, c.insertions) == (1, 0, 1)
    c = edit_counts(list("abc"), [])
    assert (c.deletions, c.rate) == (3, 1.0)
    assert edit_counts([], list("ab")).insertions == 2


def test_mixed_error_rate_ignores_punctuation_and_width() -> None:
    r = mixed_error_rate("你好，世界！Hello", "你好世界 hello")
    assert r.errors == 0 and r.ref_len == 5
    r = mixed_error_rate("我玩Townfall", "我玩town fall")
    assert (r.substitutions, r.insertions) == (1, 1)


def test_lenient_rate_drops_fillers() -> None:
    ref = "我觉得这个好"
    hyp = "嗯我觉得啊这个呃好"
    assert mixed_error_rate(ref, hyp).insertions == 3
    assert mixed_error_rate(ref, hyp, drop_fillers=True).errors == 0
    assert mixed_error_rate("好吧", "好", drop_fillers=True).deletions == 1  # 吧 is kept


def test_term_recall() -> None:
    ref = "我玩《寂静岭》，寂静岭真吓人，还有jump scare和Nadya"
    hyp = "我玩寂静林，寂静岭真吓人，还有 Jump-Scare 和娜迪亚"
    tr = term_recall(ref, hyp, ["寂静岭", "jump scare", "Nadya", "DOOM"])
    assert tr.per_term == {"寂静岭": (1, 2), "jump scare": (1, 1), "Nadya": (0, 1)}
    assert (tr.found, tr.expected) == (2, 4)
    assert tr.recall == 0.5


def test_merge_segments() -> None:
    segs = [(0.0, 5.0), (5.5, 12.0), (12.2, 20.0), (25.0, 26.0), (26.5, 90.0)]
    chunks = merge_segments(segs, max_len=25.0, max_gap=1.5)
    assert chunks[0] == (0.0, 20.0)
    assert chunks[1][0] == 25.0  # the 5 s gap starts a new chunk
    assert len(chunks) == 4  # the 63.5 s segment is split into three ~21 s pieces
    assert all(e - s <= 25.0 + 1e-9 for s, e in chunks)
    assert chunks[-1][1] == 90.0
