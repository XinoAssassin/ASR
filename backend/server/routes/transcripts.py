"""Transcripts (PLAN §5.3): read, proofread (M7 ops), revert, export.

Edits only ever touch ``segments`` / ``speakers``; ``original`` is immutable (rule 9).
"""

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool

from asr.correct.llm import LlmCorrector
from asr.editing import EditError, EditState, add_speakers
from asr.providers.base import SPEAKER_COLORS, Correction, ProviderError, Speaker, Transcript
from asr.writers import MEDIA_TYPES, write
from server.config import load_effective_settings
from server.db import Database
from server.deps import DbDep, EnvDep
from server.schemas import (
    EditTextOp,
    ExportFormat,
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


@router.patch("/{transcript_id}", response_model=TranscriptOut)
async def patch_transcript(transcript_id: str, body: TranscriptPatch, db: DbDep) -> TranscriptOut:
    """Apply ops in order to ``segments`` / ``speakers`` / ``corrections``; ``original`` is
    immutable (rule 9). All ops are validated before anything is written (all-or-nothing)."""
    current = await _get_or_404(db, transcript_id)
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
                    state.replace(op.find, op.replace)
                case MergeSpeakersOp():
                    state.merge_speakers(op.from_id, op.into_id)
                case ReviewCorrectionOp():
                    state.review_correction(op.correction_id, op.decision)
        except EditError as exc:
            raise HTTPException(_422, f"ops[{n}]: {exc}") from None
    updated = await db.update_transcript(
        transcript_id,
        segments=state.segments,
        speakers=state.speakers,
        corrections=state.corrections,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return updated


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
        status_code = _422 if "not configured" in str(exc) else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code, str(exc)) from None
    checked = {s.id for s in segments}
    kept = [
        c
        for c in current.corrections
        if not (c.source == "llm" and c.status == "suggested" and c.segment_id in checked)
    ]
    updated = await db.update_transcript(transcript_id, corrections=[*kept, *found])
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return LlmSuggestionsOut(transcript=updated, added=len(found), discarded=discarded)


@router.put("/{transcript_id}/speakers", response_model=TranscriptOut)
async def update_speakers(transcript_id: str, body: SpeakersUpdate, db: DbDep) -> TranscriptOut:
    """Rename / recolour speakers; an id not in the transcript yet adds a new speaker (for a
    voice diarization missed). Speakers not listed are unchanged."""
    current = await _get_or_404(db, transcript_id)
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
    speakers = [by_id[sid] for sid in order]
    updated = await db.update_transcript(transcript_id, speakers=speakers)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return updated


@router.post("/{transcript_id}/revert", response_model=TranscriptOut)
async def revert_transcript(transcript_id: str, db: DbDep) -> TranscriptOut:
    """``segments <- original``. Speaker names/colours are kept; any speaker id used by the
    original but missing from the list is added back."""
    current = await _get_or_404(db, transcript_id)
    known = {s.id for s in current.speakers}
    speakers = list(current.speakers)
    for seg in current.original:
        if seg.speaker_id not in known:
            known.add(seg.speaker_id)
            color = SPEAKER_COLORS[len(speakers) % len(SPEAKER_COLORS)]
            speakers.append(Speaker(id=seg.speaker_id, name=seg.speaker_id, color=color))
    if len(speakers) != len(current.speakers):
        await db.update_transcript(transcript_id, speakers=speakers)
    reverted = await db.revert_transcript(transcript_id)
    if reverted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return reverted


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
            },
        },
        422: {"description": "Invalid parameters, or `docx` (not implemented yet)."},
    },
)
async def export_transcript(
    transcript_id: str,
    db: DbDep,
    format: Annotated[ExportFormat | Literal["docx"], Query()] = "txt",
    timestamps: Annotated[bool, Query()] = True,
    merge_same_speaker: Annotated[bool, Query()] = False,
) -> Response:
    if format == "docx":
        raise HTTPException(_422, "docx export is not implemented yet")
    current = await _get_or_404(db, transcript_id)
    transcript = Transcript(
        provider=current.provider,
        duration=current.duration,
        language=current.language,
        speakers=current.speakers,
        segments=current.segments,
        warnings=current.warnings,
    )
    body = write(transcript, format, timestamps=timestamps, merge_same_speaker=merge_same_speaker)
    stem = await _export_stem(db, current.job_id)
    return Response(
        content=body.encode("utf-8"),
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": content_disposition(stem, format)},
    )
