"""SiYuan (思源笔记) export: settings, connection test, notebooks, push a transcript.

The backend talks to the SiYuan kernel itself, so the SiYuan API token stays server-side like
the DashScope key (AGENTS.md rule 3): it is write-only here, masked on read, sent only to the
configured endpoint (no redirects followed) and never logged. SiYuan must therefore be reachable
from the backend host. Findings: ``docs/research/siyuan-export.md``.
"""

import logging
from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from asr.export import siyuan
from asr.export.markdown import doc_tags, render_markdown, siyuan_doc_attrs
from server import config
from server.config import SiYuanSettings, load_siyuan_settings, mask_secret
from server.deps import DbDep, EnvDep
from server.routes.transcripts import _get_or_404, export_source
from server.schemas import (
    SiYuanNotebookOut,
    SiYuanPushIn,
    SiYuanPushOut,
    SiYuanSettingsOut,
    SiYuanSettingsUpdate,
    SiYuanTestOut,
)

router = APIRouter(tags=["siyuan"])
log = logging.getLogger(__name__)

_422 = status.HTTP_422_UNPROCESSABLE_CONTENT
_502 = status.HTTP_502_BAD_GATEWAY

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
NOT_CONFIGURED = "SiYuan is not configured: set the SiYuan API token in Settings"
NO_NOTEBOOK = "no SiYuan notebook: choose one in Settings or pass `notebook`"
REACH_HINT = (
    "The ASR backend connects to SiYuan itself, so SiYuan must be reachable from the backend "
    "host: run both on the same machine, or enable SiYuan's network serving (with an access "
    "authorization code) and set its LAN address as the endpoint."
)


def _settings_out(s: SiYuanSettings) -> SiYuanSettingsOut:
    return SiYuanSettingsOut(
        endpoint=s.endpoint,
        token_set=s.token_set,
        token_masked=mask_secret(s.token),
        notebook=s.notebook,
        path_template=s.path_template,
        path_fields=list(siyuan.PATH_FIELDS),
    )


async def _call[T](s: SiYuanSettings, fn: Callable[[siyuan.SiYuanClient], T]) -> T:
    """Run ``fn`` with a client for the configured endpoint in a worker thread."""

    def run() -> T:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as http:
            return fn(siyuan.SiYuanClient(s.endpoint, s.token.get_secret_value(), http=http))

    return await run_in_threadpool(run)


def _upstream_error(action: str, exc: siyuan.SiYuanError) -> HTTPException:
    # SiYuanError messages never contain the token (asr.export.siyuan).
    log.warning("SiYuan %s failed: %s", action, exc)
    return HTTPException(_502, f"{exc}. {REACH_HINT}")


# -------------------------------------------------------------------------------------- settings
@router.get("/api/siyuan/settings", response_model=SiYuanSettingsOut)
async def get_siyuan_settings(db: DbDep) -> SiYuanSettingsOut:
    return _settings_out(await load_siyuan_settings(db))


@router.put("/api/siyuan/settings", response_model=SiYuanSettingsOut)
async def update_siyuan_settings(body: SiYuanSettingsUpdate, db: DbDep) -> SiYuanSettingsOut:
    """Partial update; ``""`` resets a field. ``token`` is write-only. A changed ``endpoint``
    without a ``token`` in the same request clears the stored token."""
    current = await load_siyuan_settings(db)
    writes: dict[str, str | None] = {}  # None deletes the row
    if body.endpoint is not None:
        raw = body.endpoint.strip()
        try:
            endpoint = siyuan.normalize_endpoint(raw) if raw else siyuan.DEFAULT_ENDPOINT
        except ValueError as exc:
            raise HTTPException(_422, f"endpoint: {exc}") from None
        writes[config.SETTING_SIYUAN_ENDPOINT] = endpoint if raw else None
        if endpoint != current.endpoint and body.token is None and current.token_set:
            writes[config.SETTING_SIYUAN_TOKEN] = None
    if body.token is not None:
        token = body.token.get_secret_value().strip()
        if token and (any(c.isspace() for c in token) or not token.isprintable()):
            raise HTTPException(_422, "token must not contain whitespace")
        writes[config.SETTING_SIYUAN_TOKEN] = token or None
    if body.notebook is not None:
        notebook = body.notebook.strip()
        if notebook and not siyuan.is_block_id(notebook):
            raise HTTPException(_422, "notebook must be a SiYuan notebook id")
        writes[config.SETTING_SIYUAN_NOTEBOOK] = notebook or None
    if body.path_template is not None:
        raw = body.path_template.strip()
        try:
            writes[config.SETTING_SIYUAN_PATH_TEMPLATE] = (
                siyuan.validate_path_template(raw) if raw else None
            )
        except ValueError as exc:
            raise HTTPException(_422, str(exc)) from None
    for key, value in writes.items():
        if value:
            await db.set_setting(key, value)
        else:
            await db.delete_setting(key)
    return _settings_out(await load_siyuan_settings(db))


@router.post("/api/siyuan/test", response_model=SiYuanTestOut)
async def test_siyuan(db: DbDep) -> SiYuanTestOut:
    """Check the saved endpoint and token: kernel version and notebook list. Note that SiYuan
    does not check the token for loopback requests when it has no access authorization code."""
    s = await load_siyuan_settings(db)
    if not s.token_set:
        return SiYuanTestOut(ok=False, message=NOT_CONFIGURED)

    def probe(client: siyuan.SiYuanClient) -> tuple[str, int]:
        return client.version(), len(client.notebooks())

    try:
        version, count = await _call(s, probe)
    except siyuan.SiYuanError as exc:
        log.warning("SiYuan connection test failed: %s", exc)
        return SiYuanTestOut(ok=False, message=f"{exc}. {REACH_HINT}")
    return SiYuanTestOut(
        ok=True,
        message=f"connected to SiYuan {version or '(unknown version)'}, {count} notebooks",
        version=version or None,
        notebooks=count,
    )


@router.get("/api/siyuan/notebooks", response_model=list[SiYuanNotebookOut])
async def list_siyuan_notebooks(db: DbDep) -> list[SiYuanNotebookOut]:
    s = await load_siyuan_settings(db)
    if not s.token_set:
        raise HTTPException(_422, NOT_CONFIGURED)
    try:
        books = await _call(s, lambda client: client.notebooks())
    except siyuan.SiYuanError as exc:
        raise _upstream_error("notebook list", exc) from None
    return [SiYuanNotebookOut(id=b.id, name=b.name, closed=b.closed) for b in books]


# ------------------------------------------------------------------------------------------ push
@router.post(
    "/api/transcripts/{transcript_id}/siyuan",
    response_model=SiYuanPushOut,
    responses={
        404: {"description": "Transcript not found."},
        422: {"description": "SiYuan not configured, no notebook, or an invalid path."},
        502: {"description": "SiYuan unreachable or refused the request."},
    },
)
async def push_to_siyuan(
    transcript_id: str, body: SiYuanPushIn, db: DbDep, env: EnvDep
) -> SiYuanPushOut:
    """Create a SiYuan document from the current segments (SiYuan-flavoured Markdown: summary,
    speaker table, one block per turn with ``custom-asr-speaker`` / ``-start`` / ``-end``
    attributes) and set ``custom-asr-*`` document attributes and tags. Every call creates a new
    document; an existing path gets a `` (2)`` suffix."""
    current = await _get_or_404(db, transcript_id)
    s = await load_siyuan_settings(db)
    if not s.token_set:
        raise HTTPException(_422, NOT_CONFIGURED)
    notebook = body.notebook or s.notebook
    if not notebook:
        raise HTTPException(_422, NO_NOTEBOOK)
    transcript, meta, _stem = await export_source(db, env, current)
    try:
        if body.path is not None and body.path.strip():
            path = siyuan.normalize_path(body.path)
        else:
            created = meta.created_at
            path = siyuan.render_path(
                s.path_template,
                {
                    "title": meta.title or "",
                    "date": created.strftime("%Y-%m-%d") if created else "",
                    "time": created.strftime("%H:%M") if created else "",
                    "source": meta.source_name or "",
                    "provider": transcript.provider,
                },
            )
    except ValueError as exc:
        raise HTTPException(_422, f"path: {exc}") from None
    markdown = render_markdown(
        transcript,
        meta,
        flavor="siyuan",
        locale=body.locale,
        timestamps=body.timestamps,
        merge_same_speaker=body.merge_same_speaker,
    )
    attrs = siyuan_doc_attrs(transcript, meta, locale=body.locale)
    tags = ",".join(t.replace(",", " ") for t in doc_tags(meta, locale=body.locale))
    try:
        result = await _call(
            s, lambda client: client.push(notebook, path, markdown, attrs=attrs, tags=tags)
        )
    except siyuan.SiYuanError as exc:
        raise _upstream_error("push", exc) from None
    for warning in result.warnings:
        log.warning("SiYuan push: %s", warning)
    return SiYuanPushOut(
        doc_id=result.doc_id,
        notebook=notebook,
        path=result.path,
        url=f"siyuan://blocks/{result.doc_id}",
        warnings=result.warnings,
    )
