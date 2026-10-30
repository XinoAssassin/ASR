"""Cross-layer offset fixture (ARCH_REVIEW §4-e): core ``EditState``, the PATCH route and the SPA's
UTF-16 conversion all run ``tests/fixtures/offsets.json``.

JSON shape (``version`` 1)::

    {
      "_doc": str, "version": 1,
      "initial": {                       # one transcript every case starts from
        "speakers":    [Speaker],        # API shape
        "segments":    [Segment],        # API shape (id, speaker_id, start, end, text)
        "corrections": [Correction & {"offset_utf16": int}]
      },
      "cases": [{
        "name": str, "description": str,
        "ops": [TranscriptOp],           # exactly the PATCH body's ``ops`` (code-point positions)
        "expect":                        # either the whole resulting state ...
          {"segments":    [{"id", "text", "edited"}],          # in order, all segments
           "corrections": [{"id", "segment_id", "status",
                            "offset": int | null,              # code points (D57)
                            "offset_utf16": int | null}]}      # same position in a JS string
          | {"error": str}               # ... or the EditError message (PATCH: 422, no write)
      }],
      "positions": [{"text": str, "pairs": [[utf16_index, code_point_index], ...]}]
                                         # every code-point boundary of each initial text
    }

Texts mix CJK, astral emoji / CJK Extension B (surrogate pairs in UTF-16), Latin words and
corrections, so code-point and UTF-16 positions differ. The frontend counterpart is
``frontend/src/utils/offsets.fixture.test.ts``. Regenerate expectations only on a deliberate
behaviour change, and review the diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from asr.editing import EditError, EditState
from asr.providers.base import Correction, Segment, Speaker, Transcript
from server.config import EnvSettings
from server.schemas import TranscriptPatch
from tests.conftest import EnvFactory
from tests.test_jobs import DASHSCOPE_KEY, FakeProvider, add_upload, install, wait_status

FIXTURE = Path(__file__).parent / "fixtures" / "offsets.json"
DATA: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
INITIAL: dict[str, Any] = DATA["initial"]
CASES: list[dict[str, Any]] = DATA["cases"]
CASE_IDS = [c["name"] for c in CASES]


def utf16_index(text: str, code_point: int) -> int:
    """JavaScript string index of code point ``code_point`` (what ``codePointToUtf16`` returns)."""
    return len(text[:code_point].encode("utf-16-le")) // 2


def _corrections() -> list[Correction]:
    return [
        Correction(**{k: v for k, v in c.items() if k != "offset_utf16"})
        for c in INITIAL["corrections"]
    ]


def _state() -> EditState:
    segments = [Segment(**s) for s in INITIAL["segments"]]
    return EditState(
        segments=segments,
        speakers=[Speaker(**s) for s in INITIAL["speakers"]],
        corrections=_corrections(),
        original_text={s.id: s.text for s in segments},
    )


def _apply(state: EditState, ops: list[dict[str, Any]]) -> None:
    for op in TranscriptPatch.model_validate({"ops": ops}).ops:
        match op.op:
            case "split":
                state.split(op.segment_id, op.at)
            case "replace":
                state.replace(op.find, op.replace, ignore_case=op.ignore_case)
            case "review_correction":
                state.review_correction(op.correction_id, op.decision)
            case "edit_text":
                state.edit_text(op.segment_id, op.text)
            case other:  # pragma: no cover - add a branch when the fixture grows
                raise AssertionError(f"fixture op {other!r} not handled")


def _observed(segments: list[dict[str, Any]], corrections: list[dict[str, Any]]) -> dict[str, Any]:
    texts = {s["id"]: s["text"] for s in segments}
    return {
        "segments": [{"id": s["id"], "text": s["text"], "edited": s["edited"]} for s in segments],
        "corrections": [
            {
                "id": c["id"],
                "segment_id": c["segment_id"],
                "status": c["status"],
                "offset": c["offset"],
                "offset_utf16": None
                if c["offset"] is None
                else utf16_index(texts[c["segment_id"]], c["offset"]),
            }
            for c in corrections
        ],
    }


# --------------------------------------------------------------------------- fixture sanity
def test_fixture_is_self_consistent() -> None:
    texts = {s["id"]: s["text"] for s in INITIAL["segments"]}
    for c in INITIAL["corrections"]:
        text = texts[c["segment_id"]]
        shown = c["after"] if c["status"] in ("applied", "accepted") else c["before"]
        assert text[c["offset"] : c["offset"] + len(shown)] == shown
        assert c["offset_utf16"] == utf16_index(text, c["offset"])
    assert {p["text"] for p in DATA["positions"]} == set(texts.values())
    for entry in DATA["positions"]:
        text = entry["text"]
        assert entry["pairs"] == [[utf16_index(text, cp), cp] for cp in range(len(text) + 1)]
    # The fixture must actually exercise surrogate pairs.
    assert any(utf16_index(t, len(t)) != len(t) for t in texts.values())
    # Every expected offset points at the correction's visible text.
    by_id = {c["id"]: c for c in INITIAL["corrections"]}
    for case in CASES:
        expect = case["expect"]
        if "error" in expect:
            continue
        seg_text = {s["id"]: s["text"] for s in expect["segments"]}
        for c in expect["corrections"]:
            if c["offset"] is None:
                continue
            src = by_id[c["id"]]
            shown = src["after"] if c["status"] in ("applied", "accepted") else src["before"]
            text = seg_text[c["segment_id"]]
            assert text[c["offset"] : c["offset"] + len(shown)] == shown, case["name"]


# ---------------------------------------------------------------------------- core EditState
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_edit_state(case: dict[str, Any]) -> None:
    state = _state()
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(EditError, match=expect["error"]):
            _apply(state, case["ops"])
        return
    _apply(state, case["ops"])
    observed = _observed(
        [s.model_dump() for s in state.segments], [c.model_dump() for c in state.corrections]
    )
    assert observed == expect


# ------------------------------------------------------------------------------ PATCH route
@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


async def _seed(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> dict[str, Any]:
    """A stored transcript whose ``original`` / ``segments`` / ``corrections`` are the fixture's."""
    result = Transcript(
        provider="cloud",
        duration=10.0,
        language="zh",
        speakers=[Speaker(**s) for s in INITIAL["speakers"]],
        segments=[Segment(**s) for s in INITIAL["segments"]],
    )
    install(app, FakeProvider(result=result))
    upload = await add_upload(app, env)
    body = {
        "upload_id": upload.id,
        "options": {"correction": {"terms": False, "repetition": False}},
    }
    job = (await client.post("/api/jobs", json=body)).json()
    done = await wait_status(client, job["id"], {"succeeded"})
    stored = await app.state.db.update_transcript(done["transcript_id"], corrections=_corrections())
    assert stored is not None
    return stored.model_dump(mode="json")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_patch_route(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, case: dict[str, Any]
) -> None:
    seeded = await _seed(app, client, env)
    url = f"/api/transcripts/{seeded['id']}"
    resp = await client.patch(url, json={"ops": case["ops"]})
    expect = case["expect"]
    if "error" in expect:
        assert resp.status_code == 422, resp.text
        assert expect["error"] in resp.json()["detail"]
        after = (await client.get(url)).json()
        assert after["segments"] == seeded["segments"]  # all-or-nothing
        assert after["corrections"] == seeded["corrections"]
        return
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _observed(body["segments"], body["corrections"]) == expect
    assert body["original"] == seeded["original"]  # rule 9
