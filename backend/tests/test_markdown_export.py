"""``asr.export.markdown``: the Markdown note template, escaping and the SiYuan flavour."""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from asr.export.markdown import (
    ExportMeta,
    escape_inline,
    escape_line,
    render_markdown,
    siyuan_doc_attrs,
)
from asr.providers.base import Segment, Speaker, Transcript
from asr.summarize import ActionItem, TranscriptSummary
from asr.writers import MEDIA_TYPES, write

CST = timezone(timedelta(hours=8))


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(
        provider="local",
        duration=2032.4,
        language="zh",
        speakers=[
            Speaker(id="SPEAKER_00", name="张三", color="#1f77b4"),
            Speaker(id="SPEAKER_01", name="李四", color="#ff7f0e"),
        ],
        segments=[
            Segment(
                id="a", speaker_id="SPEAKER_00", start=192.3, end=200.0, text="先过一下 FPGA。"
            ),
            Segment(
                id="b", speaker_id="SPEAKER_01", start=220.5, end=230.0, text="好，先说 timing"
            ),
            Segment(id="c", speaker_id="SPEAKER_01", start=230.5, end=236.0, text="的问题"),
            Segment(id="d", speaker_id="SPEAKER_02", start=3600.0, end=3610.0, text="1. 好的"),
        ],
        warnings=["Speaker count is an estimate"],
    )


@pytest.fixture
def meta() -> ExportMeta:
    summary = TranscriptSummary(
        overview="讨论了 timing 问题。\n\n下周复测。",
        key_points=["timing 收敛"],
        decisions=["下周复测"],
        action_items=[
            ActionItem(task="整理报告", owner="张三", due="周五"),
            ActionItem(task="联系供应商"),
        ],
        model="qwen-plus",
        created_at=datetime(2026, 10, 29, 7, tzinfo=UTC),
    )
    return ExportMeta(
        title="周会 10-29",
        source_name="周会 10-29.m4a",
        created_at=datetime(2026, 10, 29, 14, 0, 5, 123, tzinfo=CST),
        model="qwen3-asr-1.7b",
        transcript_id="91424c3b",
        summary=summary,
    )


GOLDEN = """\
---
title: "周会 10-29"
date: 2026-10-29T14:00:05+08:00
duration: "00:33:52"
source: "周会 10-29.m4a"
provider: "local"
model: "qwen3-asr-1.7b"
language: "zh"
speakers: ["张三", "李四", "SPEAKER_02"]
transcript_id: "91424c3b"
tags: ["会议记录"]
---

# 周会 10-29

> 2026-10-29 14:00 · 时长 33:52 · 3 位发言人 · 本地识别（qwen3-asr-1.7b）

## 摘要

> 由 qwen-plus 自动生成，仅供参考。

讨论了 timing 问题。

下周复测。

### 要点

- timing 收敛

### 决定

- 下周复测

### 待办

- [ ] 整理报告（负责人：张三 · 期限：周五）
- [ ] 联系供应商

## 发言人

| 发言人 | 发言次数 | 时长 | 占比 |
| --- | ---: | ---: | ---: |
| 李四 | 1 | 00:15 | 46% |
| SPEAKER_02 | 1 | 00:10 | 31% |
| 张三 | 1 | 00:08 | 24% |

## 文字稿

**张三** `00:03:12`\\
先过一下 FPGA。

**李四** `00:03:40`\\
好，先说 timing

**李四** `00:03:50`\\
的问题

**SPEAKER_02** `01:00:00`\\
1\\. 好的

## 备注

- Speaker count is an estimate
"""


def test_golden_zh(transcript: Transcript, meta: ExportMeta) -> None:
    assert render_markdown(transcript, meta) == GOLDEN
    assert write(transcript, "md", meta=meta) == GOLDEN
    assert MEDIA_TYPES["md"].startswith("text/markdown")


def test_deterministic(transcript: Transcript, meta: ExportMeta) -> None:
    assert render_markdown(transcript, meta) == render_markdown(transcript, meta)


def test_english_merge_no_timestamps(transcript: Transcript, meta: ExportMeta) -> None:
    out = write(transcript, "md", meta=meta, locale="en", timestamps=False, merge_same_speaker=True)
    assert (
        "> 2026-10-29 14:00 · duration 33:52 · 3 speakers · local recognition (qwen3-asr-1.7b)"
        in out
    )
    assert "## Summary" in out and "### Action items" in out and "## Transcript" in out
    assert "- [ ] 整理报告 (owner: 张三 · due: 周五)" in out
    assert "| Speaker | Turns | Talk time | Share |" in out
    assert "**李四**\\\n好，先说 timing的问题\n" in out  # one turn, no time
    assert out.count("**李四**") == 1
    assert "`00:" not in out
    assert 'tags: ["meeting-notes"]' in out


def test_stale_summary_and_defaults(transcript: Transcript, meta: ExportMeta) -> None:
    stale = dataclasses.replace(meta, summary_stale=True)
    assert "内容可能已过时" in render_markdown(transcript, stale)
    plain = render_markdown(transcript)  # no metadata at all
    assert plain.startswith('---\ntitle: "会议记录"\nduration: "00:33:52"\nprovider: "local"\n')
    assert "## 摘要" not in plain
    untitled = ExportMeta(created_at=datetime(2026, 10, 29, 9, tzinfo=CST))
    assert "# 会议记录 2026-10-29\n" in render_markdown(transcript, untitled)


def test_empty_transcript() -> None:
    out = render_markdown(Transcript(provider="cloud"))
    assert "## 文字稿\n\n（无内容）\n" in out
    assert "## 发言人" not in out and "## 备注" not in out
    assert "0 位发言人 · 云端识别" in out


# -------------------------------------------------------------------------------------- escaping
@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("# 标题", "\\# 标题"),
        ("> 引用", "\\> 引用"),
        ("- 列表", "\\- 列表"),
        ("+ 列表", "\\+ 列表"),
        ("* 列表", "\\* 列表"),
        ("1. 第一", "1\\. 第一"),
        ("12) 第二", "12\\) 第二"),
        ("===", "\\=\\=\\="),
        ("---", "\\---"),
        ("```python", "\\`\\`\\`python"),
        ("~~~", "\\~\\~\\~"),
        ("| a | b |", "\\| a \\| b \\|"),
        ("<div>x</div>", "\\<div>x\\</div>"),
        ('{: id="x"}', '\\{: id="x"}'),
        ("   缩进", "缩进"),
    ],
)
def test_escape_line_block_syntax(raw: str, escaped: str) -> None:
    assert escape_line(raw) == escaped


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("*强调* 和 **粗体**", "\\*强调\\* 和 \\*\\*粗体\\*\\*"),
        ("_斜体_", "\\_斜体\\_"),
        ("SPEAKER_00 snake_case", "SPEAKER_00 snake_case"),  # intraword: never emphasis
        ("`code`", "\\`code\\`"),
        ("[链接](http://x)", "\\[链接](http://x)"),
        ("![图](a.png)", "!\\[图](a.png)"),
        ("a\\b", "a\\\\b"),
        ("$x^2$", "\\$x\\^2\\$"),
        ("#标签 #tag#", "\\#标签 \\#tag\\#"),
        ("==高亮==", "\\=\\=高亮\\=\\="),
        ("a = b", "a = b"),
        ("((20210817205410-2kvfpfn))", "\\((20210817205410-2kvfpfn))"),
        ("{{SELECT}}", "\\{\\{SELECT}}"),
        ("R&D &amp; &#35;", "R&D \\&amp; \\&\\#35;"),
        ("1~2 天", "1\\~2 天"),
        ("2 > 1，好的。English text.", "2 > 1，好的。English text."),
    ],
)
def test_escape_inline(raw: str, escaped: str) -> None:
    assert escape_inline(raw) == escaped


def test_hostile_text_cannot_create_structure() -> None:
    t = Transcript(
        provider="cloud",
        speakers=[Speaker(id="S", name="**Eve** | x\n# boss", color="#1f77b4")],
        segments=[
            Segment(id="1", speaker_id="S", start=0, end=1, text="ok\n\n# 标题\n- 列表\n```"),
            Segment(id="2", speaker_id="S", start=1, end=2, text="---"),
        ],
        warnings=["# warn\n- two"],
    )
    out = render_markdown(t)
    body = out.split("## 文字稿\n\n", 1)[1]
    # Every turn is exactly two lines: the name line and one text line.
    turns = body.split("\n\n## 备注")[0].split("\n\n")
    assert turns == [
        "**\\*\\*Eve\\*\\* \\| x \\# boss** `00:00:00`\\\nok \\# 标题 - 列表 \\`\\`\\`",
        "**\\*\\*Eve\\*\\* \\| x \\# boss** `00:00:01`\\\n\\---",
    ]
    assert "- \\# warn - two\n" in out
    assert "| \\*\\*Eve\\*\\* \\| x \\# boss | 1 | 00:02 | 100% |" in out  # one cell
    assert 'speakers: ["**Eve** | x # boss"]' in out


def test_yaml_values_are_quoted_and_escaped() -> None:
    meta = ExportMeta(
        title='a: "b"\n- c',
        source_name="x\u2028y\x85z.m4a",
        model="m'1",
        tags=("会议记录", "a: b"),
    )
    out = render_markdown(Transcript(provider="cloud"), meta)
    assert 'title: "a: \\"b\\" - c"\n' in out
    assert 'source: "x y z.m4a"\n' in out  # U+2028 / NEL are line breaks: collapsed
    assert 'model: "m\'1"\n' in out
    assert 'tags: ["会议记录", "a: b"]\n' in out
    front = out.split("---\n")[1]
    assert all(line.split(": ", 1)[0].isidentifier() for line in front.splitlines())


# ---------------------------------------------------------------------------------------- SiYuan
def test_siyuan_flavour(transcript: Transcript, meta: ExportMeta) -> None:
    out = render_markdown(transcript, meta, flavor="siyuan")
    assert not out.startswith("---") and "\n# " not in out and "title:" not in out
    assert out.startswith("> 2026-10-29 14:00 · 时长 33:52")
    assert (
        "**张三** `00:03:12`\n先过一下 FPGA。\n"
        '{: custom-asr-speaker="张三" custom-asr-speaker-id="SPEAKER_00" '
        'custom-asr-start="192.30" custom-asr-end="200.00"}\n'
    ) in out
    assert "\\\n" not in out  # plain line breaks, no backslash hard breaks


def test_siyuan_ial_values_escaped() -> None:
    t = Transcript(
        provider="cloud",
        speakers=[Speaker(id="S", name='a" b="c', color="#1f77b4")],
        segments=[Segment(id="1", speaker_id="S", start=0, end=1, text="x")],
    )
    out = render_markdown(t, flavor="siyuan")
    assert 'custom-asr-speaker="a&quot; b=&quot;c"' in out


def test_siyuan_doc_attrs(transcript: Transcript, meta: ExportMeta) -> None:
    assert siyuan_doc_attrs(transcript, meta) == {
        "custom-asr-title": "周会 10-29",
        "custom-asr-date": "2026-10-29T14:00:05+08:00",
        "custom-asr-duration": "00:33:52",
        "custom-asr-source": "周会 10-29.m4a",
        "custom-asr-provider": "local",
        "custom-asr-model": "qwen3-asr-1.7b",
        "custom-asr-language": "zh",
        "custom-asr-speakers": "张三, 李四, SPEAKER_02",
        "custom-asr-transcript-id": "91424c3b",
    }
    assert siyuan_doc_attrs(Transcript(provider="cloud")) == {
        "custom-asr-title": "会议记录",
        "custom-asr-provider": "cloud",
    }
