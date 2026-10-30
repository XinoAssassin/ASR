"""Golden tests for ``asr.writers``."""

import json

import pytest

from asr.providers.base import Segment, Speaker, Transcript
from asr.writers import merge_consecutive, render, write


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(
        provider="cloud",
        duration=3725.0,
        language="zh",
        speakers=[
            Speaker(id="SPEAKER_00", name="张三", color="#1f77b4"),
            Speaker(id="SPEAKER_01", name="Li <Si>", color="#ff7f0e"),
        ],
        segments=[
            Segment(id="a", speaker_id="SPEAKER_00", start=3.2, end=8.7, text="今天我们先过一下。"),
            Segment(id="b", speaker_id="SPEAKER_01", start=9.1, end=12.44, text="好，先说 FPGA"),
            Segment(id="c", speaker_id="SPEAKER_01", start=12.5, end=14.0, text="timing 的问题"),
            Segment(id="d", speaker_id="SPEAKER_02", start=3600.05, end=3725.0, text="a & b"),
        ],
    )


def test_txt(transcript: Transcript) -> None:
    assert write(transcript, "txt") == (
        "[00:00:03.2 - 00:00:08.7] 张三: 今天我们先过一下。\n"
        "[00:00:09.1 - 00:00:12.4] Li <Si>: 好，先说 FPGA\n"
        "[00:00:12.5 - 00:00:14.0] Li <Si>: timing 的问题\n"
        "[01:00:00.1 - 01:02:05.0] SPEAKER_02: a & b\n"
    )


def test_txt_without_timestamps(transcript: Transcript) -> None:
    assert write(transcript, "txt", timestamps=False) == (
        "张三: 今天我们先过一下。\n"
        "Li <Si>: 好，先说 FPGA\n"
        "Li <Si>: timing 的问题\n"
        "SPEAKER_02: a & b\n"
    )


def test_txt_merge_same_speaker(transcript: Transcript) -> None:
    assert write(transcript, "txt", merge_same_speaker=True) == (
        "[00:00:03.2 - 00:00:08.7] 张三: 今天我们先过一下。\n"
        "[00:00:09.1 - 00:00:14.0] Li <Si>: 好，先说 FPGA timing 的问题\n"
        "[01:00:00.1 - 01:02:05.0] SPEAKER_02: a & b\n"
    )


def test_srt(transcript: Transcript) -> None:
    assert write(transcript, "srt") == (
        "1\n00:00:03,200 --> 00:00:08,700\n张三: 今天我们先过一下。\n\n"
        "2\n00:00:09,100 --> 00:00:12,440\nLi <Si>: 好，先说 FPGA\n\n"
        "3\n00:00:12,500 --> 00:00:14,000\nLi <Si>: timing 的问题\n\n"
        "4\n01:00:00,050 --> 01:02:05,000\nSPEAKER_02: a & b\n"
    )


def test_vtt(transcript: Transcript) -> None:
    assert write(transcript, "vtt") == (
        "WEBVTT\n\n"
        "00:00:03.200 --> 00:00:08.700\n<v 张三>今天我们先过一下。\n\n"
        "00:00:09.100 --> 00:00:12.440\n<v Li &lt;Si&gt;>好，先说 FPGA\n\n"
        "00:00:12.500 --> 00:00:14.000\n<v Li &lt;Si&gt;>timing 的问题\n\n"
        "01:00:00.050 --> 01:02:05.000\n<v SPEAKER_02>a &amp; b\n"
    )


def test_vtt_merge(transcript: Transcript) -> None:
    out = write(transcript, "vtt", merge_same_speaker=True)
    assert out.count("-->") == 3
    assert "00:00:09.100 --> 00:00:14.000\n<v Li &lt;Si&gt;>好，先说 FPGA timing 的问题\n" in out


def test_json(transcript: Transcript) -> None:
    out = write(transcript, "json")
    assert "张三" in out  # ensure_ascii=False
    data = json.loads(out)
    assert Transcript.model_validate(data) == transcript


def test_json_merge(transcript: Transcript) -> None:
    data = json.loads(write(transcript, "json", merge_same_speaker=True))
    assert [s["id"] for s in data["segments"]] == ["a", "b", "d"]


def test_merge_joining_rules() -> None:
    segs = [
        Segment(id="1", speaker_id="A", start=0, end=1, text="你好"),
        Segment(id="2", speaker_id="A", start=1, end=2, text="世界", edited=True),
        Segment(id="3", speaker_id="A", start=2, end=3, text="hello"),
        Segment(id="4", speaker_id="A", start=3, end=4, text="world."),
        Segment(id="5", speaker_id="A", start=4, end=5, text="Next"),
        Segment(id="6", speaker_id="B", start=5, end=6, text="x"),
    ]
    merged = merge_consecutive(segs)
    assert [m.text for m in merged] == ["你好世界hello world. Next", "x"]
    assert merged[0].edited is True and merged[0].start == 0 and merged[0].end == 5
    assert segs[0].text == "你好"  # inputs untouched


def test_empty_and_multiline() -> None:
    empty = Transcript(provider="cloud")
    assert write(empty, "txt") == ""
    assert write(empty, "srt") == ""
    assert write(empty, "vtt") == "WEBVTT\n"
    multi = Transcript(
        provider="cloud",
        segments=[Segment(id="1", speaker_id="A", start=0, end=1, text="line1\n\nline2")],
    )
    assert "A: line1 line2\n" in write(multi, "srt")


def test_render_alias_and_bad_format(transcript: Transcript) -> None:
    assert render is write
    with pytest.raises(ValueError, match="unsupported"):
        write(transcript, "pdf")  # type: ignore[arg-type]


def test_speaker_names_with_line_breaks_stay_on_one_line() -> None:
    t = Transcript(
        provider="cloud",
        speakers=[
            Speaker(
                id="SPEAKER_00", name="张三\n\n2\n00:00:00,000 --> 00:00:09,000", color="#1f77b4"
            )
        ],
        segments=[Segment(id="a", speaker_id="SPEAKER_00", start=0.0, end=1.0, text="你好")],
    )
    assert (
        write(t, "txt") == "[00:00:00.0 - 00:00:01.0] 张三 2 00:00:00,000 --> 00:00:09,000: 你好\n"
    )
    assert write(t, "srt").count("\n") == 3  # index, span, one text line
    vtt = write(t, "vtt")
    assert "<v 张三 2 00:00:00,000 --&gt; 00:00:09,000>你好" in vtt
    assert vtt.count("\n") == 4


def test_md(transcript: Transcript) -> None:
    out = write(transcript, "md")
    assert out.startswith('---\ntitle: "会议记录"\nduration: "01:02:05"\nprovider: "cloud"\n')
    assert "**Li \\<Si>** `00:00:09`\\\n好，先说 FPGA\n" in out
    assert "**SPEAKER_02** `01:00:00`\\\na & b\n" in out
    merged = write(transcript, "md", merge_same_speaker=True, timestamps=False)
    assert "**Li \\<Si>**\\\n好，先说 FPGA timing 的问题\n" in merged
    assert merged.count("**Li \\<Si>**") == 1
    assert "## Speakers" in write(transcript, "md", locale="en")
