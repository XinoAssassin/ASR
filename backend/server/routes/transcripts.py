"""Transcripts (PLAN §5.3). Owned by ``backend-jobs`` (M1)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from server.schemas import ExportFormat, SpeakersUpdate, TranscriptOut, TranscriptPatch

router = APIRouter(prefix="/api/transcripts", tags=["transcripts"])


@router.get("/{transcript_id}", response_model=TranscriptOut)
async def get_transcript(transcript_id: str) -> TranscriptOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.patch("/{transcript_id}", response_model=TranscriptOut)
async def patch_transcript(transcript_id: str, body: TranscriptPatch) -> TranscriptOut:
    """Apply ops to ``segments`` only; ``original`` is immutable (rule 8)."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.put("/{transcript_id}/speakers", response_model=TranscriptOut)
async def update_speakers(transcript_id: str, body: SpeakersUpdate) -> TranscriptOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/{transcript_id}/revert", response_model=TranscriptOut)
async def revert_transcript(transcript_id: str) -> TranscriptOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


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
        }
    },
)
async def export_transcript(
    transcript_id: str,
    format: Annotated[ExportFormat, Query()] = "txt",
    timestamps: Annotated[bool, Query()] = True,
    merge_same_speaker: Annotated[bool, Query()] = False,
) -> Response:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
