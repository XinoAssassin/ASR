"""``/api/transcripts`` routes: edit ops, speakers, revert, export."""

from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI

from server.config import EnvSettings
from tests.conftest import EnvFactory
from tests.test_jobs import DASHSCOPE_KEY, FakeProvider, add_upload, install, wait_status


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


async def _make_transcript(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings, name: str = "会议记录 3月.m4a"
) -> dict[str, Any]:
    install(app, FakeProvider())
    upload = await add_upload(app, env, name)
    job = (await client.post("/api/jobs", json={"upload_id": upload.id})).json()
    done = await wait_status(client, job["id"], {"succeeded"})
    return (await client.get(f"/api/transcripts/{done['transcript_id']}")).json()


@pytest.fixture
async def tr(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> dict[str, Any]:
    return await _make_transcript(app, client, env)


async def test_get_unknown(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/transcripts/nope")).status_code == 404
    patch = {"ops": [{"op": "edit_text", "segment_id": "s0", "text": "x"}]}
    assert (await client.patch("/api/transcripts/nope", json=patch)).status_code == 404
    assert (await client.post("/api/transcripts/nope/revert")).status_code == 404
    assert (await client.get("/api/transcripts/nope/export")).status_code == 404


async def test_edit_text_and_set_speaker(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    url = f"/api/transcripts/{tr['id']}"
    original = tr["original"]
    resp = await client.patch(
        url,
        json={
            "ops": [
                {"op": "edit_text", "segment_id": "s0", "text": "改过的文字"},
                {"op": "set_speaker", "segment_id": "s1", "speaker_id": "SPEAKER_00"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    s0, s1 = body["segments"][0], body["segments"][1]
    assert s0["text"] == "改过的文字" and s0["edited"] is True
    assert s1["speaker_id"] == "SPEAKER_00"
    assert body["original"] == original  # immutable

    # Editing back to the recognised text clears the flag.
    body = (
        await client.patch(
            url,
            json={"ops": [{"op": "edit_text", "segment_id": "s0", "text": original[0]["text"]}]},
        )
    ).json()
    assert body["segments"][0]["edited"] is False
    assert (await client.get(url)).json()["original"] == original


@pytest.mark.parametrize(
    "op",
    [
        {"op": "edit_text", "segment_id": "missing", "text": "x"},
        {"op": "set_speaker", "segment_id": "s0", "speaker_id": "SPEAKER_99"},
        {"op": "split", "segment_id": "s0"},
    ],
)
async def test_bad_ops_are_atomic(
    client: httpx.AsyncClient, tr: dict[str, Any], op: dict[str, Any]
) -> None:
    url = f"/api/transcripts/{tr['id']}"
    ops = [{"op": "edit_text", "segment_id": "s0", "text": "should not persist"}, op]
    assert (await client.patch(url, json={"ops": ops})).status_code == 422
    assert (await client.get(url)).json()["segments"] == tr["segments"]


async def test_update_speakers(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    url = f"/api/transcripts/{tr['id']}/speakers"
    resp = await client.put(
        url, json={"speakers": [{"id": "SPEAKER_01", "name": "李四", "color": "#00aa00"}]}
    )
    assert resp.status_code == 200
    speakers = {s["id"]: s for s in resp.json()["speakers"]}
    assert speakers["SPEAKER_01"] == {"id": "SPEAKER_01", "name": "李四", "color": "#00aa00"}
    assert speakers["SPEAKER_00"]["name"] == "SPEAKER_00"

    # Name only keeps the colour.
    resp = await client.put(url, json={"speakers": [{"id": "SPEAKER_01", "name": "老李"}]})
    assert resp.json()["speakers"][1]["color"] == "#00aa00"

    # An unknown id adds a speaker (a voice diarization missed); it gets a palette colour.
    resp = await client.put(url, json={"speakers": [{"id": "SPEAKER_77", "name": "王五"}]})
    added = resp.json()["speakers"][-1]
    assert (
        added["id"] == "SPEAKER_77" and added["name"] == "王五" and added["color"].startswith("#")
    )

    bad = [
        {"speakers": [{"id": "SPEAKER_00", "name": "x", "color": "red"}]},
        {"speakers": [{"id": "SPEAKER_00", "name": "a"}, {"id": "SPEAKER_00", "name": "b"}]},
        {"speakers": [{"id": "SPEAKER_00", "name": "   "}]},
    ]
    for body in bad:
        assert (await client.put(url, json=body)).status_code == 422, body


async def test_revert(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    url = f"/api/transcripts/{tr['id']}"
    await client.patch(
        url,
        json={
            "ops": [
                {"op": "edit_text", "segment_id": "s2", "text": "x"},
                {"op": "set_speaker", "segment_id": "s0", "speaker_id": "SPEAKER_01"},
            ]
        },
    )
    await client.put(f"{url}/speakers", json={"speakers": [{"id": "SPEAKER_00", "name": "张三"}]})
    resp = await client.post(f"{url}/revert")
    assert resp.status_code == 200
    body = resp.json()
    assert body["segments"] == tr["original"] == body["original"]
    # Display names survive a revert; every speaker id is still listed.
    assert body["speakers"][0]["name"] == "张三"
    assert {s["speaker_id"] for s in body["segments"]} <= {s["id"] for s in body["speakers"]}


# -------------------------------------------------------------------------------------- export
@pytest.mark.parametrize(
    ("fmt", "media", "marker"),
    [
        ("txt", "text/plain", "[00:00:00.0 - 00:00:02.5] SPEAKER_00: 今天"),
        ("srt", "application/x-subrip", "00:00:00,000 --> 00:00:02,500"),
        ("vtt", "text/vtt", "WEBVTT"),
        ("json", "application/json", '"provider": "cloud"'),
    ],
)
async def test_export_formats(
    client: httpx.AsyncClient, tr: dict[str, Any], fmt: str, media: str, marker: str
) -> None:
    resp = await client.get(f"/api/transcripts/{tr['id']}/export", params={"format": fmt})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(media)
    assert marker in resp.text
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert f"filename*=UTF-8''{quote(f'会议记录 3月.{fmt}', safe='')}" in cd
    cd.encode("latin-1")  # header must be pure ASCII/latin-1
    assert f'filename="3.{fmt}"' in cd  # ASCII fallback keeps what it can


async def test_export_uses_display_names_and_options(
    client: httpx.AsyncClient, tr: dict[str, Any]
) -> None:
    await client.put(
        f"/api/transcripts/{tr['id']}/speakers",
        json={"speakers": [{"id": "SPEAKER_01", "name": "李四"}]},
    )
    resp = await client.get(
        f"/api/transcripts/{tr['id']}/export",
        params={"format": "txt", "timestamps": "false", "merge_same_speaker": "true"},
    )
    assert resp.text == (
        "SPEAKER_00: 今天我们先过一下测试结果。\n李四: 好，我先说 chip 部分。然后是 driver。\n"
    )


async def test_export_docx_not_implemented(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    resp = await client.get(f"/api/transcripts/{tr['id']}/export", params={"format": "docx"})
    assert resp.status_code == 422
    assert "not implemented" in resp.json()["detail"]
    resp = await client.get(f"/api/transcripts/{tr['id']}/export", params={"format": "pdf"})
    assert resp.status_code == 422


async def test_export_ascii_name(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> None:
    tr = await _make_transcript(app, client, env, name='weekly "sync".m4a')
    resp = await client.get(f"/api/transcripts/{tr['id']}/export", params={"format": "srt"})
    cd = resp.headers["content-disposition"]
    assert 'filename="weekly _sync.srt"' in cd
