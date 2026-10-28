"""Transcripts (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Edits only ever touch ``segments`` / ``speakers``; ``original`` is immutable (rule 8).
"""

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Response, status

from asr.providers.base import SPEAKER_COLORS, Speaker, Transcript
from asr.writers import MEDIA_TYPES, write
from server.db import Database
from server.deps import DbDep
from server.schemas import (
    EditTextOp,
    ExportFormat,
    SpeakersUpdate,
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
    """Apply ops to ``segments`` only; ``original`` is immutable (rule 8).

    All ops are validated before anything is written (all-or-nothing).
    """
    current = await _get_or_404(db, transcript_id)
    segments = [s.model_copy() for s in current.segments]
    index = {s.id: i for i, s in enumerate(segments)}
    original_text = {s.id: s.text for s in current.original}
    speaker_ids = {s.id for s in current.speakers}

    for n, op in enumerate(body.ops):
        i = index.get(op.segment_id)
        if i is None:
            raise HTTPException(_422, f"ops[{n}]: unknown segment {op.segment_id!r}")
        seg = segments[i]
        if isinstance(op, EditTextOp):
            # ``edited`` reflects whether the text differs from the recognition result.
            edited = op.text != original_text.get(seg.id)
            segments[i] = seg.model_copy(update={"text": op.text, "edited": edited})
        else:
            if op.speaker_id not in speaker_ids:
                raise HTTPException(_422, f"ops[{n}]: unknown speaker {op.speaker_id!r}")
            segments[i] = seg.model_copy(update={"speaker_id": op.speaker_id})

    updated = await db.update_transcript(transcript_id, segments=segments)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "transcript not found")
    return updated


@router.put("/{transcript_id}/speakers", response_model=TranscriptOut)
async def update_speakers(transcript_id: str, body: SpeakersUpdate, db: DbDep) -> TranscriptOut:
    """Rename / recolour existing speakers; speakers not listed are unchanged."""
    current = await _get_or_404(db, transcript_id)
    by_id = {s.id: s for s in current.speakers}
    seen: set[str] = set()
    for n, upd in enumerate(body.speakers):
        if upd.id in seen:
            raise HTTPException(_422, f"speakers[{n}]: duplicate id {upd.id!r}")
        seen.add(upd.id)
        if upd.id not in by_id:
            raise HTTPException(_422, f"speakers[{n}]: unknown speaker {upd.id!r}")
        name = upd.name.strip()
        if not name:
            raise HTTPException(_422, f"speakers[{n}]: name must not be blank")
        prev = by_id[upd.id]
        by_id[upd.id] = Speaker(id=prev.id, name=name, color=upd.color or prev.color)
    speakers = [by_id[s.id] for s in current.speakers]
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
