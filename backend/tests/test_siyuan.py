"""SiYuan export: ``asr.export.siyuan`` client and the ``/api/siyuan`` routes (mocked kernel)."""

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from asr.export import siyuan
from asr.export.siyuan import SiYuanClient, SiYuanError
from server.config import EnvSettings
from tests.conftest import EnvFactory
from tests.test_jobs import DASHSCOPE_KEY
from tests.test_routes_transcripts import _make_transcript

SIYUAN = "http://127.0.0.1:6806"
TOKEN = "siyuan-token-SECRET-1234"
NOTEBOOK = "20210817205410-2kvfpfn"
DOC_ID = "20261030120000-abcdefg"
PARENT_ID = "20261030115959-parent1"


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "", "data": data})


class FakeKernel:
    """Enough of the SiYuan kernel for a push: hpath index, createDocWithMd, setBlockAttrs."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.docs: dict[str, str] = dict(existing or {})  # hpath -> id
        self.created: list[dict[str, Any]] = []
        self.attrs: list[dict[str, Any]] = []
        self.auth: list[str | None] = []
        self.attr_error = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization"))
        body = json.loads(request.content or b"{}")
        api = request.url.path
        if api == "/api/system/version":
            return ok("3.4.0")
        if api == "/api/notebook/lsNotebooks":
            return ok(
                {
                    "notebooks": [
                        {"id": NOTEBOOK, "name": "工作", "icon": "", "sort": 0, "closed": False},
                        {"id": "20210808180117-czj9bvb", "name": "旧", "closed": True},
                        {"id": "not-an-id", "name": "bogus"},
                    ]
                }
            )
        if api == "/api/filetree/getIDsByHPath":
            found = self.docs.get(body["path"])
            return ok([found] if found else [])
        if api == "/api/filetree/createDocWithMd":
            self.created.append(body)
            doc_id = DOC_ID if body["markdown"] else PARENT_ID
            self.docs[body["path"]] = doc_id
            return ok(doc_id)
        if api == "/api/attr/setBlockAttrs":
            if self.attr_error:
                return httpx.Response(200, json={"code": -1, "msg": "block not found"})
            self.attrs.append(body)
            return ok(None)
        return httpx.Response(404)


@pytest.fixture
def kernel() -> FakeKernel:
    return FakeKernel()


@pytest.fixture
def mock_siyuan(kernel: FakeKernel) -> respx.MockRouter:
    with respx.mock(base_url=SIYUAN, assert_all_called=False) as router:
        router.route(method="POST").mock(side_effect=kernel)
        yield router


def _app_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Everything logged, except aiosqlite's DEBUG trace of SQL parameters (a settings write
    necessarily carries the token; see the report on ``logging_setup`` quiet loggers)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name != "aiosqlite")


def client(token: str = TOKEN) -> SiYuanClient:
    return SiYuanClient(SIYUAN, token, http=httpx.Client(follow_redirects=False))


# ------------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" http://127.0.0.1:6806/ ", "http://127.0.0.1:6806"),
        ("https://notes.lan:443/siyuan", "https://notes.lan:443/siyuan"),
    ],
)
def test_normalize_endpoint(raw: str, expected: str) -> None:
    assert siyuan.normalize_endpoint(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://x",
        "file:///etc/passwd",
        "127.0.0.1:6806",
        "http://user:pw@host",
        "http://host?x=1",
        "http://host:bad",
        "",
    ],
)
def test_normalize_endpoint_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        siyuan.normalize_endpoint(raw)


def test_path_template() -> None:
    assert siyuan.validate_path_template(" /会议记录/{date} {title} ") == "/会议记录/{date} {title}"
    for bad in ("会议记录/{title}", "/{title.__class__}", "/{nope}", "/{title", "/x}"):
        with pytest.raises(ValueError):
            siyuan.validate_path_template(bad)
    values = {"date": "2026-10-29", "title": "A/B\n周会", "source": "", "provider": "local"}
    assert (
        siyuan.render_path("/会议记录/{date} {title}", values) == "/会议记录/2026-10-29 A／B 周会"
    )
    assert siyuan.render_path("/{source}/{title}", values) == "/A／B 周会"  # empty level dropped
    with pytest.raises(ValueError):
        siyuan.render_path("/{source}", values)
    assert siyuan.normalize_path(" /a//b \n c/ ") == "/a/b c"
    with pytest.raises(ValueError):
        siyuan.normalize_path("a/b")


# ----------------------------------------------------------------------------------- client
def test_token_header_and_notebooks(mock_siyuan: respx.MockRouter, kernel: FakeKernel) -> None:
    books = client().notebooks()
    assert [(b.id, b.name, b.closed) for b in books] == [
        (NOTEBOOK, "工作", False),
        ("20210808180117-czj9bvb", "旧", True),
    ]
    assert kernel.auth == [f"Token {TOKEN}"]
    assert mock_siyuan.calls.last.request.headers["content-type"] == "application/json"


def test_push_creates_parents_and_picks_a_free_path(
    mock_siyuan: respx.MockRouter, kernel: FakeKernel
) -> None:
    kernel.docs["/会议记录/周会"] = "20200101000000-exist01"  # parent exists, target too
    kernel.docs["/会议记录/周会 (2)"] = "20200101000000-exist02"
    result = client().push(
        NOTEBOOK, "/会议记录/周会", "# x", attrs={"custom-asr-provider": "local"}, tags="会议记录"
    )
    assert result.doc_id == DOC_ID and result.path == "/会议记录/周会 (3)" and not result.warnings
    # "/会议记录" did not exist: created (empty) before the document.
    assert [(c["path"], c["markdown"]) for c in kernel.created] == [
        ("/会议记录", ""),
        ("/会议记录/周会 (3)", "# x"),
    ]
    assert kernel.created[1]["notebook"] == NOTEBOOK and kernel.created[1]["tags"] == "会议记录"
    assert kernel.attrs == [{"id": DOC_ID, "attrs": {"custom-asr-provider": "local"}}]


def test_attr_failure_is_a_warning(mock_siyuan: respx.MockRouter, kernel: FakeKernel) -> None:
    kernel.attr_error = True
    result = client().push(NOTEBOOK, "/x", "body", attrs={"custom-a": "b"})
    assert result.doc_id == DOC_ID
    assert result.warnings and "block not found" in result.warnings[0]


def test_redirect_is_not_followed() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{SIYUAN}/api/notebook/lsNotebooks").mock(
            return_value=httpx.Response(307, headers={"Location": "http://evil.test/steal"})
        )
        evil = router.route(host="evil.test").mock(return_value=ok({"notebooks": []}))
        # Even a client configured to follow redirects must not: each request opts out.
        http = httpx.Client(follow_redirects=True)
        with pytest.raises(SiYuanError, match="redirect") as info:
            SiYuanClient(SIYUAN, TOKEN, http=http).notebooks()
        assert not evil.called
        assert TOKEN not in str(info.value)


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(401, json={"code": -1, "msg": "Auth failed"}), "API token"),
        (httpx.Response(500, text="boom"), "HTTP 500"),
        (httpx.Response(200, text="<html>router login</html>"), "did not answer like a SiYuan"),
        (
            httpx.Response(200, json={"code": -1, "msg": f"bad token {TOKEN}"}),
            "bad token \\*\\*\\*\\*",
        ),
    ],
)
def test_errors_are_redacted(response: httpx.Response, match: str) -> None:
    with respx.mock(base_url=SIYUAN) as router:
        router.post("/api/system/version").mock(return_value=response)
        with pytest.raises(SiYuanError, match=match) as info:
            client().version()
    assert TOKEN not in str(info.value)


def test_unreachable_and_timeout() -> None:
    with respx.mock(base_url=SIYUAN) as router:
        router.post("/api/system/version").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(SiYuanError, match=r"cannot reach SiYuan at http://127\.0\.0\.1:6806"):
            client().version()
        router.post("/api/system/version").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(SiYuanError, match="did not answer in time"):
            client().version()


def test_bad_create_answer() -> None:
    with respx.mock(base_url=SIYUAN) as router:
        router.post("/api/filetree/createDocWithMd").mock(return_value=ok("../../etc"))
        with pytest.raises(SiYuanError, match="unexpected answer"):
            client().create_doc(NOTEBOOK, "/x", "y")


def test_response_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(siyuan, "MAX_RESPONSE_BYTES", 100)
    with respx.mock(base_url=SIYUAN) as router:
        router.post("/api/system/version").mock(return_value=ok("x" * 500))
        with pytest.raises(SiYuanError, match="too large"):
            client().version()


# ----------------------------------------------------------------------------------- routes
async def _configure(c: httpx.AsyncClient, **extra: Any) -> dict[str, Any]:
    resp = await c.put("/api/siyuan/settings", json={"token": TOKEN, **extra})
    assert resp.status_code == 200, resp.text
    assert TOKEN not in resp.text
    return resp.json()


async def test_settings_defaults_and_masking(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/siyuan/settings")).json()
    assert body == {
        "endpoint": "http://127.0.0.1:6806",
        "token_set": False,
        "token_masked": None,
        "notebook": None,
        "path_template": "/会议记录/{date} {title}",
        "path_fields": ["title", "date", "time", "source", "provider"],
    }
    body = await _configure(client, notebook=NOTEBOOK, path_template="/ASR/{date}")
    assert body["token_set"] is True and body["token_masked"] == "****1234"
    assert body["notebook"] == NOTEBOOK and body["path_template"] == "/ASR/{date}"
    assert TOKEN not in (await client.get("/api/siyuan/settings")).text

    # Reset fields with "".
    body = (
        await client.put("/api/siyuan/settings", json={"notebook": "", "path_template": ""})
    ).json()
    assert body["notebook"] is None and body["path_template"] == "/会议记录/{date} {title}"
    assert body["token_set"] is True
    body = (await client.put("/api/siyuan/settings", json={"token": ""})).json()
    assert body["token_set"] is False


async def test_changing_endpoint_clears_token(client: httpx.AsyncClient) -> None:
    await _configure(client)
    # Same endpoint (normalised): token kept.
    body = (
        await client.put("/api/siyuan/settings", json={"endpoint": "http://127.0.0.1:6806/"})
    ).json()
    assert body["token_set"] is True
    # Another host without a new token: the old token must not follow it.
    body = (
        await client.put("/api/siyuan/settings", json={"endpoint": "http://evil.test:6806"})
    ).json()
    assert body["endpoint"] == "http://evil.test:6806" and body["token_set"] is False
    # Endpoint and token together: kept.
    body = await _configure(client, endpoint="http://192.168.1.5:6806")
    assert body["endpoint"] == "http://192.168.1.5:6806" and body["token_set"] is True


@pytest.mark.parametrize(
    "update",
    [
        {"endpoint": "ftp://x"},
        {"endpoint": "http://u:p@x"},
        {"token": "a b"},
        {"notebook": "../x"},
        {"path_template": "no-slash"},
        {"path_template": "/{evil.__init__}"},
    ],
)
async def test_settings_validation(client: httpx.AsyncClient, update: dict[str, str]) -> None:
    resp = await client.put("/api/siyuan/settings", json=update)
    assert resp.status_code == 422, resp.text


async def test_not_configured(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    assert (await client.get("/api/siyuan/notebooks")).status_code == 422
    resp = await client.post(f"/api/transcripts/{tr['id']}/siyuan", json={})
    assert resp.status_code == 422 and "not configured" in resp.json()["detail"]
    body = (await client.post("/api/siyuan/test")).json()
    assert body["ok"] is False and "not configured" in body["message"]
    # Token but no notebook.
    await _configure(client)
    resp = await client.post(f"/api/transcripts/{tr['id']}/siyuan", json={})
    assert resp.status_code == 422 and "notebook" in resp.json()["detail"]
    resp = await client.post(f"/api/transcripts/{tr['id']}/siyuan", json={"notebook": "x"})
    assert resp.status_code == 422
    resp = await client.post("/api/transcripts/nope/siyuan", json={})
    assert resp.status_code == 404


async def test_test_and_notebooks(
    client: httpx.AsyncClient, mock_siyuan: respx.MockRouter, kernel: FakeKernel
) -> None:
    await _configure(client)
    body = (await client.post("/api/siyuan/test")).json()
    assert body == {
        "ok": True,
        "message": "connected to SiYuan 3.4.0, 2 notebooks",
        "version": "3.4.0",
        "notebooks": 2,
    }
    books = (await client.get("/api/siyuan/notebooks")).json()
    assert books[0] == {"id": NOTEBOOK, "name": "工作", "closed": False}
    assert set(kernel.auth) == {f"Token {TOKEN}"}


@pytest.fixture
async def tr(app: FastAPI, client: httpx.AsyncClient, env: EnvSettings) -> dict[str, Any]:
    return await _make_transcript(app, client, env)


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)


async def test_push(
    client: httpx.AsyncClient,
    tr: dict[str, Any],
    mock_siyuan: respx.MockRouter,
    kernel: FakeKernel,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await _configure(client, notebook=NOTEBOOK)
    await client.put(
        f"/api/transcripts/{tr['id']}/speakers",
        json={"speakers": [{"id": "SPEAKER_01", "name": "李四"}]},
    )
    resp = await client.post(
        f"/api/transcripts/{tr['id']}/siyuan", json={"merge_same_speaker": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_id"] == DOC_ID and body["notebook"] == NOTEBOOK
    assert body["url"] == f"siyuan://blocks/{DOC_ID}" and body["warnings"] == []
    assert body["path"].startswith("/会议记录/") and body["path"].endswith(" 会议记录 3月")
    assert TOKEN not in resp.text
    assert set(kernel.auth) == {f"Token {TOKEN}"}  # every call, incl. lookups

    doc = kernel.created[-1]
    assert doc["path"] == body["path"] and doc["tags"] == "会议记录"
    md = doc["markdown"]
    assert not md.startswith("---") and "\n# " not in md
    assert "**李四** `00:00:02`\n好，我先说 chip 部分。然后是 driver。\n" in md
    assert 'custom-asr-speaker="李四" custom-asr-speaker-id="SPEAKER_01"' in md
    attrs = kernel.attrs[-1]
    assert attrs["id"] == DOC_ID
    assert attrs["attrs"]["custom-asr-transcript-id"] == tr["id"]
    assert attrs["attrs"]["custom-asr-source"] == "会议记录 3月.m4a"
    assert attrs["attrs"]["custom-asr-speakers"] == "SPEAKER_00, 李四"
    assert TOKEN not in _app_logs(caplog)

    # Explicit path and English labels; the same path again gets a suffix.
    for expected in ("/ASR/周会", "/ASR/周会 (2)"):
        resp = await client.post(
            f"/api/transcripts/{tr['id']}/siyuan",
            json={"path": "/ASR/周会", "locale": "en", "notebook": NOTEBOOK},
        )
        assert resp.json()["path"] == expected
    assert "## Transcript" in kernel.created[-1]["markdown"]
    assert kernel.created[-1]["tags"] == "meeting-notes"


async def test_push_unreachable_is_502_without_token(
    client: httpx.AsyncClient, tr: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _configure(client, notebook=NOTEBOOK)
    with respx.mock(base_url=SIYUAN) as router:
        router.route().mock(side_effect=httpx.ConnectError(f"refused {TOKEN}"))
        resp = await client.post(f"/api/transcripts/{tr['id']}/siyuan", json={})
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "cannot reach SiYuan" in detail and "reachable from the backend host" in detail
        assert TOKEN not in resp.text
        assert (await client.get("/api/siyuan/notebooks")).status_code == 502
        test = (await client.post("/api/siyuan/test")).json()
        assert test["ok"] is False and TOKEN not in test["message"]
    assert TOKEN not in _app_logs(caplog)


async def test_push_redirect_is_502(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    await _configure(client, notebook=NOTEBOOK)
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=SIYUAN).mock(
            return_value=httpx.Response(302, headers={"Location": "http://evil.test/"})
        )
        evil = router.route(host="evil.test").mock(return_value=ok([]))
        resp = await client.post(f"/api/transcripts/{tr['id']}/siyuan", json={})
    assert resp.status_code == 502 and "redirect" in resp.json()["detail"]
    assert not evil.called


# ------------------------------------------------------------------------------- md download
async def test_export_md_route(client: httpx.AsyncClient, tr: dict[str, Any]) -> None:
    resp = await client.get(
        f"/api/transcripts/{tr['id']}/export", params={"format": "md", "locale": "en"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert ".md" in resp.headers["content-disposition"]
    text = resp.text
    assert text.startswith('---\ntitle: "会议记录 3月"\n')
    assert 'source: "会议记录 3月.m4a"\n' in text
    assert f'transcript_id: "{tr["id"]}"\n' in text
    assert "\n# 会议记录 3月\n" in text and "## Transcript" in text
    assert "**SPEAKER_00** `00:00:00`\\\n今天我们先过一下测试结果。\n" in text
    bad = await client.get(
        f"/api/transcripts/{tr['id']}/export", params={"format": "md", "locale": "fr"}
    )
    assert bad.status_code == 422
