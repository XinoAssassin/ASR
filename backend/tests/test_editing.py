"""M7 editing ops: split / merge / replace / merge_speakers / review_correction (+ PATCH route)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from asr.correct import correct_segments
from asr.editing import EditError, EditState
from asr.providers.base import Correction, CorrectionOptions, Segment, Speaker, Term, Word
from server.config import EnvSettings
from tests.conftest import EnvFactory
from tests.test_jobs import FakeProvider, add_upload, install, wait_status

SPK = [
    Speaker(id="SPEAKER_00", name="主持人", color="#1f77b4"),
    Speaker(id="SPEAKER_01", name="嘉宾", color="#ff7f0e"),
]


def seg(
    sid: str,
    start: float,
    end: float,
    text: str,
    speaker: str = "SPEAKER_00",
    words: list[Word] | None = None,
) -> Segment:
    return Segment(id=sid, speaker_id=speaker, start=start, end=end, text=text, words=words or [])


def state(*segments: Segment, corrections: list[Correction] | None = None) -> EditState:
    return EditState(
        segments=list(segments),
        speakers=list(SPK),
        corrections=list(corrections or []),
        original_text={s.id: s.text for s in segments},
    )


def test_split_uses_word_timestamps() -> None:
    words = [
        Word(text=c, start=i * 0.5, end=i * 0.5 + 0.4) for i, c in enumerate("大家好我是主持人")
    ]
    st = state(seg("a", 0.0, 4.0, "大家好，我是主持人。", words=words))
    new_id = st.split("a", 4)  # before "我"
    first, second = st.segments
    assert (first.text, second.text) == ("大家好，", "我是主持人。")
    assert first.end == second.start == 1.5  # the start of "我"
    assert [w.text for w in first.words] == list("大家好")
    assert second.id == new_id == "a-1" and first.edited and second.edited


def test_split_without_words_interpolates() -> None:
    st = state(seg("a", 10.0, 20.0, "前半句后半句"))
    st.split("a", 3)
    assert st.segments[0].end == st.segments[1].start == 15.0
    with pytest.raises(EditError):
        st.split("a", 99)  # nothing left on the right


def test_merge_with_next_in_time_order() -> None:
    st = state(
        seg("b", 5.0, 6.0, "world", speaker="SPEAKER_01"),
        seg("a", 0.0, 1.0, "hello"),
        seg("c", 7.0, 8.0, "你好"),
    )
    st.merge("a")
    assert [s.id for s in st.segments] == ["a", "c"]
    merged = st.segments[0]
    assert (merged.text, merged.end, merged.speaker_id) == ("hello world", 6.0, "SPEAKER_00")
    st.merge("a")
    assert st.segments[0].text == "hello world你好"
    with pytest.raises(EditError, match="last segment"):
        st.merge("a")


def test_replace_and_edited_flag() -> None:
    st = state(seg("a", 0, 1, "汤泡很好玩"), seg("b", 1, 2, "汤泡汤泡"), seg("c", 2, 3, "没有"))
    assert st.replace("汤泡", "Townfall") == 3
    assert [s.text for s in st.segments] == ["Townfall很好玩", "TownfallTownfall", "没有"]
    assert [s.edited for s in st.segments] == [True, True, False]
    assert st.replace("Townfall", "汤泡") == 3
    assert not any(s.edited for s in st.segments)  # back to the recognised text


def test_merge_speakers() -> None:
    st = state(seg("a", 0, 1, "x", "SPEAKER_01"), seg("b", 1, 2, "y"))
    st.merge_speakers("SPEAKER_01", "SPEAKER_00")
    assert {s.speaker_id for s in st.segments} == {"SPEAKER_00"}
    assert [s.id for s in st.speakers] == ["SPEAKER_00"]
    with pytest.raises(EditError):
        st.merge_speakers("SPEAKER_00", "SPEAKER_00")


def test_review_correction() -> None:
    applied = Correction(
        id="c1", segment_id="a", source="term", before="汤泡", after="Townfall", offset=0
    )
    suggested = Correction(
        id="c2",
        segment_id="a",
        source="llm",
        before="很好玩",
        after="真好玩",
        status="suggested",
        offset=8,
    )
    st = state(seg("a", 0, 1, "Townfall很好玩"), corrections=[applied, suggested])
    st.original_text["a"] = "汤泡很好玩"
    st.review_correction("c2", "accept")
    assert st.segments[0].text == "Townfall真好玩"
    assert st.corrections[0].offset == 0  # before the accepted change: unmoved
    st.review_correction("c1", "reject")
    assert st.segments[0].text == "汤泡真好玩"
    assert [c.status for c in st.corrections] == ["rejected", "accepted"]
    with pytest.raises(EditError, match="already reviewed"):
        st.review_correction("c1", "accept")


def _corrected(text: str, terms: list[Term] | None = None) -> EditState:
    """State of one segment ``a`` after the [5b] L1 pass over ``text``."""
    raw = [seg("a", 0.0, 1.0, text)]
    out, corrections = correct_segments(raw, terms or [], CorrectionOptions())
    st = state(*out, corrections=corrections)
    st.original_text["a"] = text
    return st


def test_reject_repetition_restores_at_its_offset() -> None:
    raw = "这个问题我觉得这个这个这个这个这个很好"
    st = _corrected(raw)
    assert st.segments[0].text == "这个问题我觉得这个很好"
    (c,) = st.corrections
    assert c.offset == 7
    st.review_correction(c.id, "reject")
    assert st.segments[0].text == raw and not st.segments[0].edited


def test_reject_term_restores_the_corrected_occurrence_only() -> None:
    raw = "Kubernetes 和库伯内特斯"
    st = _corrected(raw, [Term(term="Kubernetes", aliases=["库伯内特斯"])])
    assert st.segments[0].text == "Kubernetes 和Kubernetes"
    (c,) = st.corrections
    assert c.offset == 12
    st.review_correction(c.id, "reject")
    assert st.segments[0].text == raw


def test_review_is_strict_about_offset() -> None:
    wrong = Correction(
        id="c1", segment_id="a", source="term", before="汤泡", after="Townfall", offset=3
    )
    missing = Correction(id="c2", segment_id="a", source="term", before="汤泡", after="Townfall")
    st = state(seg("a", 0, 1, "Townfall说Townfall"), corrections=[wrong, missing])
    for cid in ("c1", "c2"):
        with pytest.raises(EditError, match="no longer matches"):
            st.review_correction(cid, "reject")
    assert st.segments[0].text == "Townfall说Townfall"
    assert [c.status for c in st.corrections] == ["applied", "applied"]
    st.review_correction("c2", "accept")  # status-only decisions still work
    assert st.corrections[1].status == "accepted"


def test_text_edits_shift_correction_offsets() -> None:
    c = Correction(id="c", segment_id="a", source="term", before="汤泡", after="Townfall", offset=9)
    st = state(seg("a", 0, 1, "Townfall说Townfall"), corrections=[c])
    st.edit_text("a", "嗯，Townfall说Townfall")  # insertion before the correction
    assert st.corrections[0].offset == 11
    st.replace("说", "讲了")  # one char longer, before it again
    assert st.corrections[0].offset == 12
    st.edit_text("a", "嗯，Townfall讲了Townfall。")  # after it: unmoved
    assert st.corrections[0].offset == 12
    st.review_correction("c", "reject")
    assert st.segments[0].text == "嗯，Townfall讲了汤泡。"


def test_accept_shifts_other_suggestions() -> None:
    first = Correction(
        id="c1", segment_id="a", source="llm", before="汤泡", after="Townfall", offset=0
    )
    second = Correction(
        id="c2",
        segment_id="a",
        source="llm",
        before="酷",
        after="cool",
        offset=3,
        status="suggested",
    )
    first = first.model_copy(update={"status": "suggested"})
    st = state(seg("a", 0, 1, "汤泡很酷，酷"), corrections=[first, second])
    st.review_correction("c1", "accept")
    assert st.corrections[1].offset == 9
    st.review_correction("c2", "accept")
    assert st.segments[0].text == "Townfall很cool，酷"


def test_edit_inside_a_correction_makes_it_unreviewable() -> None:
    c = Correction(id="c", segment_id="a", source="term", before="汤泡", after="Townfall", offset=2)
    st = state(seg("a", 0, 1, "就是Townfall吧"), corrections=[c])
    st.edit_text("a", "就是Townfell吧")
    assert st.corrections[0].offset is None
    with pytest.raises(EditError, match="no longer matches"):
        st.review_correction("c", "reject")
    assert st.segments[0].text == "就是Townfell吧"


def test_replace_ignore_case_counts_real_matches() -> None:
    c = Correction(
        id="c", segment_id="a", source="term", before="酷伯", after="Kubernetes", offset=18
    )
    st = state(seg("a", 0, 1, "Townfall townfall Kubernetes"), corrections=[c])
    assert st.replace("townfall", "x") == 1  # case-sensitive by default
    assert st.corrections[0].offset == 11
    assert st.replace("TOWNFALL", "T", ignore_case=True) == 1
    assert st.replace("X", "T", ignore_case=True) == 1
    assert st.segments[0].text == "T T Kubernetes"
    assert st.corrections[0].offset == 4
    st.review_correction("c", "reject")
    assert st.segments[0].text == "T T 酷伯"


def test_split_moves_corrections_and_drops_straddling_offsets() -> None:
    right = Correction(id="r", segment_id="a", source="term", before="酷伯", after="Kube", offset=9)
    straddle = Correction(
        id="s", segment_id="a", source="repetition", before="好好好好好好", after="好好", offset=5
    )
    left = Correction(id="l", segment_id="a", source="term", before="汤泡", after="Town", offset=0)
    st = state(seg("a", 0, 10, "Town很好好吗 Kube棒"), corrections=[right, straddle, left])
    new_id = st.split("a", 6)  # "Town很好" | "好吗 Kube棒"
    assert [s.text for s in st.segments] == ["Town很好", "好吗 Kube棒"]
    by_id = {c.id: c for c in st.corrections}
    assert (by_id["r"].segment_id, by_id["r"].offset) == (new_id, 3)
    assert (by_id["l"].segment_id, by_id["l"].offset) == ("a", 0)
    assert by_id["s"].offset is None
    st.review_correction("r", "reject")
    assert st.segments[1].text == "好吗 酷伯棒"
    with pytest.raises(EditError, match="no longer matches"):
        st.review_correction("s", "reject")
    with pytest.raises(EditError, match="both sides"):
        st.split("a", -2)  # code-point index must lie inside the text


def test_split_offset_is_a_code_point_index() -> None:
    st = state(seg("a", 0, 1, "😀好的"))  # one code point, two UTF-16 units
    st.split("a", 1)
    assert [s.text for s in st.segments] == ["😀", "好的"]


def test_merge_rebases_second_segment_corrections() -> None:
    c = Correction(id="c", segment_id="b", source="term", before="汤泡", after="Townfall", offset=1)
    st = state(seg("a", 0, 1, "hello"), seg("b", 1, 2, "玩Townfall"), corrections=[c])
    st.merge("a")
    assert (st.corrections[0].segment_id, st.corrections[0].offset) == ("a", 6)
    st.review_correction("c", "reject")
    assert st.segments[0].text == "hello玩汤泡"


# ---------------------------------------------------------------------------------- route
@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY="sk-test-key-0000000000")


async def _transcript(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> dict[str, Any]:
    from asr.providers.base import Transcript

    result = Transcript(
        provider="cloud",
        duration=10.0,
        speakers=SPK,
        segments=[
            seg("seg_00000", 0.0, 2.0, "汤泡真好玩，我们开始吧"),
            seg("seg_00001", 2.5, 4.0, "好的", "SPEAKER_01"),
        ],
    )
    install(app, FakeProvider(result=result))
    upload = await add_upload(app, env)
    body = {"upload_id": upload.id, "extra_terms": [{"term": "Townfall", "aliases": ["汤泡"]}]}
    job = (await client.post("/api/jobs", json=body)).json()
    done = await wait_status(client, job["id"], {"succeeded"})
    return (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()


async def test_patch_ops_end_to_end(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    url = f"/api/transcripts/{t['id']}"
    assert t["segments"][0]["text"].startswith("Townfall")
    correction = t["corrections"][0]
    assert correction["status"] == "applied" and correction["id"]

    ops = [
        {"op": "split", "segment_id": "seg_00000", "at": t["segments"][0]["text"].index("我")},
        {"op": "merge_speakers", "from_id": "SPEAKER_01", "into_id": "SPEAKER_00"},
        {"op": "review_correction", "correction_id": correction["id"], "decision": "reject"},
    ]
    resp = await client.patch(url, json={"ops": ops})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["text"] for s in body["segments"]] == ["汤泡真好玩，", "我们开始吧", "好的"]
    assert [s["id"] for s in body["speakers"]] == ["SPEAKER_00"]
    assert body["corrections"][0]["status"] == "rejected"
    assert body["original"][0]["text"] == "汤泡真好玩，我们开始吧"  # immutable

    resp = await client.patch(url, json={"ops": [{"op": "replace", "find": "好", "replace": "棒"}]})
    assert [s["text"] for s in resp.json()["segments"]] == ["汤泡真棒玩，", "我们开始吧", "棒的"]

    # All-or-nothing: the bad second op leaves the first unapplied.
    bad = {
        "ops": [{"op": "merge", "segment_id": "seg_00000"}, {"op": "merge", "segment_id": "nope"}]
    }
    resp = await client.patch(url, json=bad)
    assert resp.status_code == 422 and "ops[1]" in resp.json()["detail"]
    assert len((await client.get(url)).json()["segments"]) == 3

    reverted = (await client.post(f"{url}/revert")).json()
    assert [s["text"] for s in reverted["segments"]] == ["汤泡真好玩，我们开始吧", "好的"]
    assert {s["id"] for s in reverted["speakers"]} == {"SPEAKER_00", "SPEAKER_01"}
