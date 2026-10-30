"""Transcripts (PLAN §5.3): read, proofread (M7 ops), revert, export.

Edits only ever touch ``segments`` / ``speakers``; ``original`` is immutable (rule 9).
"""

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool

from asr.correct.llm import LlmCorrector
from asr.editing import EditError, EditState, add_speakers
from asr.export.markdown import ExportMeta
from asr.providers.base import SPEAKER_COLORS, Correction, ProviderError, Speaker, Transcript
from asr.summarize import Summarizer, TranscriptSummary
from asr.writers import MEDIA_TYPES, write
from server.config import EnvSettings, load_effective_settings
from server.db import Database
from server.deps import DbDep, EnvDep
from server.schemas import (
    EditTextOp,
    ExportFormat,
    ExportLocale,
    LlmSuggestionsIn,
    LlmSuggestionsOut,
    MergeOp,
    MergeSpeakersOp,
    ReplaceOp,
    ReviewCorrectionOp,
    SetSpeakerOp,
    SpeakersUpdate,
    SplitOp,
    TranscriptOut,
    TranscriptPatch,
)

router = APIRouter(prefix="/api/transcripts", tags=["transcripts"])

_422 = status.HTTP_422_UNPROCESSABLE_CONTENT
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f\x7f"\\/:*?<>|;]+')


async def _get_or_404(db: Database, transcript_id: str) -> TranscriptOut:
    transcript = await db.get_transcript(transcript_id)
    if transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return transcript


@router.get("/{transcript_id}", response_model=TranscriptOut)
async def get_transcript(transcript_id: str, db: DbDep) -> TranscriptOut:
    return await _get_or_404(db, transcript_id)


CONFLICT_DETAIL = "transcript changed since it was loaded"
MAX_WRITE_ATTEMPTS = 5
_TOLERANCE = timedelta(milliseconds=1)  # clients may round-trip updated_at through a JS Date

Compute = Callable[[TranscriptOut], dict[str, Any]]


def _same_version(expected: datetime, stored: datetime) -> bool:
    """``expected_updated_at`` matches the stored ``updated_at``. Sub-millisecond differences
    are ignored (successive writes are always >= 2 ms apart, see ``Database``)."""
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    return abs(stored - expected) < _TOLERANCE


async def _write(
    db: Database, transcript_id: str, expected: datetime | None, compute: Compute
) -> TranscriptOut:
    """Read, compute, write-if-unchanged (optimistic concurrency).

    ``compute(current)`` returns the ``update_transcript`` fields (or raises ``HTTPException``).
    With ``expected`` (the client's ``updated_at``) a change since the client loaded the
    transcript is a 409. Without it (CLI / desktop clients) a write that raced another one is
    recomputed on the fresh row, so the other write is never lost.
    """
    for _ in range(MAX_WRITE_ATTEMPTS):
        current = await _get_or_404(db, transcript_id)
        if expected is not None and not _same_version(expected, current.updated_at):
            raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT_DETAIL)
        fields = compute(current)
        updated = await db.update_transcript(
            transcript_id, **fields, if_updated_at=current.updated_at
        )
        if updated is not None:
            return updated
        # Missing (404 on the next read) or changed meanwhile (409 / recompute).
    raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT_DETAIL)


def _apply_ops(current: TranscriptOut, body: TranscriptPatch) -> dict[str, Any]:
    state = EditState(
        segments=list(current.segments),
        speakers=list(current.speakers),
        corrections=list(current.corrections),
        original_text={s.id: s.text for s in current.original},
    )
    for n, op in enumerate(body.ops):
        try:
            match op:
                case EditTextOp():
                    state.edit_text(op.segment_id, op.text)
                case SetSpeakerOp():
                    state.set_speaker(op.segment_id, op.speaker_id)
                case SplitOp():
                    state.split(op.segment_id, op.at)
                case MergeOp():
                    state.merge(op.segment_id)
                case ReplaceOp():
                    state.replace(op.find, op.replace, ignore_case=op.ignore_case)
                case MergeSpeakersOp():
                    state.merge_speakers(op.from_id, op.into_id)
                case ReviewCorrectionOp():
                    state.review_correction(op.correction_id, op.decision)
        except EditError as exc:
            raise HTTPException(_422, f"ops[{n}]: {exc}") from None
    return {
        "segments": state.segments,
        "speakers": state.speakers,
        "corrections": state.corrections,
    }


@router.patch(
    "/{transcript_id}",
    response_model=TranscriptOut,
    responses={409: {"description": "`expected_updated_at` no longer matches."}},
)
async def patch_transcript(transcript_id: str, body: TranscriptPatch, db: DbDep) -> TranscriptOut:
    """Apply ops in order to ``segments`` / ``speakers`` / ``corrections``; ``original`` is
    immutable (rule 9). All ops are validated before anything is written (all-or-nothing).
    With ``expected_updated_at`` the patch is refused (409) if the transcript changed since."""
    return await _write(
        db, transcript_id, body.expected_updated_at, lambda cur: _apply_ops(cur, body)
    )


MAX_LLM_SEGMENTS = 500


@router.post("/{transcript_id}/llm-suggestions", response_model=LlmSuggestionsOut)
async def llm_suggestions(
    transcript_id: str, body: LlmSuggestionsIn, db: DbDep, env: EnvDep
) -> LlmSuggestionsOut:
    """[5b] L3: ask the DashScope LLM for corrections of mis-heard words. Opt-in, costs money
    and sends the segment text to the cloud; results are ``suggested`` corrections for review,
    never applied automatically."""
    current = await _get_or_404(db, transcript_id)
    wanted = set(body.segment_ids) if body.segment_ids is not None else None
    segments = [s for s in current.segments if wanted is None or s.id in wanted]
    if not segments:
        raise HTTPException(_422, "no segments to check")
    if len(segments) > MAX_LLM_SEGMENTS:
        raise HTTPException(_422, f"at most {MAX_LLM_SEGMENTS} segments per request")
    settings = await load_effective_settings(env, db)
    job = await db.get_job(current.job_id)
    terms = job.options.terms if job else []
    context = job.options.context if job else None

    def run() -> tuple[list[Correction], int]:
        with httpx.Client(timeout=120, follow_redirects=False) as http:
            corrector = LlmCorrector(
                api_key=settings.cloud_api_key.get_secret_value(),
                endpoint=settings.cloud_endpoint,
                model=env.llm_model,
                http=http,
            )
            return corrector.suggest(segments, terms, context)

    try:
        found, discarded = await run_in_threadpool(run)
    except ProviderError as exc:
        raise _llm_error(exc) from None
    # The call took up to two minutes: merge into the row as it is *now*. Corrections added or
    # reviewed meanwhile are kept; segments edited meanwhile keep what they have and get none of
    # the (now misplaced) new suggestions.
    checked_text = {s.id: s.text for s in segments}
    added = 0

    def merge(fresh: TranscriptOut) -> dict[str, Any]:
        nonlocal added
        fresh_text = {s.id: s.text for s in fresh.segments}
        same = {sid for sid, text in checked_text.items() if fresh_text.get(sid) == text}
        new = [c for c in found if c.segment_id in same]
        kept = [
            c
            for c in fresh.corrections
            if not (c.source == "llm" and c.status == "suggested" and c.segment_id in same)
        ]
        added = len(new)
        return {"corrections": [*kept, *new]}

    updated = await _write(db, transcript_id, None, merge)
    return LlmSuggestionsOut(transcript=updated, added=added, discarded=discarded)


def _llm_error(exc: ProviderError) -> HTTPException:
    """Missing key / unusable input -> 422; upstream failures -> 502."""
    msg = str(exc)
    client_side = ("not configured", "allow-list", "no text", "too long")
    code = _422 if any(m in msg for m in client_side) else status.HTTP_502_BAD_GATEWAY
    return HTTPException(code, msg)


@router.get(
    "/{transcript_id}/summary",
    response_model=TranscriptSummary,
    responses={404: {"description": "Transcript not found, or no summary yet."}},
)
async def get_summary(transcript_id: str, db: DbDep) -> TranscriptSummary:
    current = await _get_or_404(db, transcript_id)
    if current.summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no summary yet")
    return current.summary


@router.post("/{transcript_id}/summary", response_model=TranscriptSummary)
async def create_summary(transcript_id: str, db: DbDep, env: EnvDep) -> TranscriptSummary:
    """M11: summarise the current segments with the DashScope LLM (overview, key points,
    decisions, action items) and store the result, replacing an earlier one. Opt-in: it costs
    money and sends the transcript text and speaker names to the cloud."""
    current = await _get_or_404(db, transcript_id)
    settings = await load_effective_settings(env, db)
    job = await db.get_job(current.job_id)
    terms = job.options.terms if job else []
    context = job.options.context if job else None

    def run() -> TranscriptSummary:
        with httpx.Client(timeout=180, follow_redirects=False) as http:
            summarizer = Summarizer(
                api_key=settings.cloud_api_key.get_secret_value(),
                endpoint=settings.cloud_endpoint,
                model=env.llm_model,
                http=http,
            )
            return summarizer.summarize(current.segments, current.speakers, terms, context)

    try:
        summary = await run_in_threadpool(run)
    except ProviderError as exc:
        raise _llm_error(exc) from None
    fresh = await _get_or_404(db, transcript_id)
    if fresh.updated_at != current.updated_at or summary.created_at < current.updated_at:
        # Edited while the LLM was working: the summary describes the text as it was loaded,
        # so date it to that version and it shows up as stale (D50).
        summary = summary.model_copy(update={"created_at": current.updated_at})
    if await db.set_summary(transcript_id, summary) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return summary


@router.delete("/{transcript_id}/summary", status_code=status.HTTP_204_NO_CONTENT)
async def delete_summary(transcript_id: str, db: DbDep) -> Response:
    if await db.set_summary(transcript_id, None) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _apply_speakers(current: TranscriptOut, body: SpeakersUpdate) -> dict[str, Any]:
    by_id = {s.id: s for s in current.speakers}
    seen: set[str] = set()
    order = [s.id for s in current.speakers]
    for n, upd in enumerate(body.speakers):
        if upd.id in seen:
            raise HTTPException(_422, f"speakers[{n}]: duplicate id {upd.id!r}")
        seen.add(upd.id)
        if upd.id not in by_id:
            by_id[upd.id] = add_speakers(list(by_id.values()), [upd.id])[-1]
            order.append(upd.id)
        name = upd.name.strip()
        if not name:
            raise HTTPException(_422, f"speakers[{n}]: name must not be blank")
        prev = by_id[upd.id]
        by_id[upd.id] = Speaker(id=prev.id, name=name, color=upd.color or prev.color)
    return {"speakers": [by_id[sid] for sid in order]}


@router.put(
    "/{transcript_id}/speakers",
    response_model=TranscriptOut,
    responses={409: {"description": "`expected_updated_at` no longer matches."}},
)
async def update_speakers(transcript_id: str, body: SpeakersUpdate, db: DbDep) -> TranscriptOut:
    """Rename / recolour speakers; an id not in the transcript yet adds a new speaker (for a
    voice diarization missed). Speakers not listed are unchanged. With ``expected_updated_at``
    the update is refused (409) if the transcript changed since."""
    return await _write(
        db, transcript_id, body.expected_updated_at, lambda cur: _apply_speakers(cur, body)
    )


def _revert(current: TranscriptOut) -> dict[str, Any]:
    known = {s.id for s in current.speakers}
    speakers = list(current.speakers)
    for seg in current.original:
        if seg.speaker_id not in known:
            known.add(seg.speaker_id)
            color = SPEAKER_COLORS[len(speakers) % len(SPEAKER_COLORS)]
            speakers.append(Speaker(id=seg.speaker_id, name=seg.speaker_id, color=color))
    # The automatic corrections no longer apply to the raw text, so they go too.
    return {"segments": current.original, "speakers": speakers, "corrections": []}


@router.post("/{transcript_id}/revert", response_model=TranscriptOut)
async def revert_transcript(transcript_id: str, db: DbDep) -> TranscriptOut:
    """``segments <- original``. Speaker names/colours are kept; any speaker id used by the
    original but missing from the list is added back."""
    return await _write(db, transcript_id, None, _revert)


# --------------------------------------------------------------------------------------- export
def _ascii_fallback(stem: str) -> str:
    folded = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    folded = _UNSAFE_FILENAME.sub("_", folded).strip(" ._")
    return folded or "transcript"


def content_disposition(stem: str, ext: str) -> str:
    """``attachment`` with an ASCII ``filename`` and an RFC 5987 ``filename*`` (UTF-8)."""
    clean = _UNSAFE_FILENAME.sub("_", stem).strip(" .") or "transcript"
    utf8 = quote(f"{clean}.{ext}", safe="")
    return f"attachment; filename=\"{_ascii_fallback(clean)}.{ext}\"; filename*=UTF-8''{utf8}"


async def _export_stem(db: Database, job_id: str) -> str:
    job = await db.get_job(job_id)
    if job is not None and job.upload_id is not None:
        upload = await db.get_upload(job.upload_id)
        if upload is not None:
            stem = PurePosixPath(upload.original_name.replace("\\", "/")).stem
            if stem:
                return stem
    if job is not None and job.file_url:
        name = PurePosixPath(unquote(urlsplit(job.file_url).path)).stem
        if name:
            return name
    return "transcript"


async def export_source(
    db: Database, env: EnvSettings, current: TranscriptOut
) -> tuple[Transcript, ExportMeta, str]:
    """The core transcript, the Markdown metadata and the file stem for an export."""
    transcript = Transcript(
        provider=current.provider,
        duration=current.duration,
        language=current.language,
        speakers=current.speakers,
        segments=current.segments,
        warnings=current.warnings,
    )
    stem = await _export_stem(db, current.job_id)
    job = await db.get_job(current.job_id)
    upload = await db.get_upload(job.upload_id) if job and job.upload_id else None
    source = None
    if upload is not None:
        source = PurePosixPath(upload.original_name.replace("\\", "/")).name or None
    elif job is not None and job.file_url:
        source = PurePosixPath(unquote(urlsplit(job.file_url).path)).name or None
    model = None
    if job is not None:
        if current.provider == "local":
            model = job.options.local.asr_model
        else:
            model = job.options.cloud.model or (await load_effective_settings(env, db)).cloud_model
    summary = current.summary
    meta = ExportMeta(
        title=None if stem == "transcript" else stem,
        source_name=source,
        # The upload time is the closest we have to the meeting time; server-local timezone.
        created_at=(upload.created_at if upload else current.created_at).astimezone(),
        model=model,
        transcript_id=current.id,
        summary=summary,
        summary_stale=summary is not None and current.updated_at > summary.created_at,
    )
    return transcript, meta, stem


@router.get(
    "/{transcript_id}/export",
    response_class=Response,
    responses={
        200: {
            "description": "Exported file (Content-Disposition: attachment).",
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/x-subrip": {"schema": {"type": "string"}},
                "text/vtt": {"schema": {"type": "string"}},
                "application/json": {"schema": {"type": "string"}},
                "text/markdown": {"schema": {"type": "string"}},
            },
        },
        422: {"description": "Invalid parameters or an unknown format."},
    },
)
async def export_transcript(
    transcript_id: str,
    db: DbDep,
    env: EnvDep,
    format: Annotated[ExportFormat, Query()] = "txt",
    timestamps: Annotated[bool, Query()] = True,
    merge_same_speaker: Annotated[bool, Query()] = False,
    locale: Annotated[
        ExportLocale, Query(description="Headings and labels of `md`; other formats ignore it.")
    ] = "zh",
) -> Response:
    """Download the current segments as txt / srt / vtt / json / md. ``md`` is a Markdown note
    (front matter, summary if any, speaker table, transcript; docs/research/siyuan-export.md)."""
    current = await _get_or_404(db, transcript_id)
    transcript, meta, stem = await export_source(db, env, current)
    body = write(
        transcript,
        format,
        timestamps=timestamps,
        merge_same_speaker=merge_same_speaker,
        locale=locale,
        meta=meta,
    )
    return Response(
        content=body.encode("utf-8"),
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": content_disposition(stem, format)},
    )
