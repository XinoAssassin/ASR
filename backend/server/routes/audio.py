"""Browser playback audio (EXECUTION_LOG D12). Owned by ``backend-core`` (M1)."""

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/audio", tags=["audio"])


@router.get(
    "/{upload_id}",
    response_class=FileResponse,
    responses={
        200: {"description": "MP3 audio.", "content": {"audio/mpeg": {}}},
        206: {"description": "Partial content (Range).", "content": {"audio/mpeg": {}}},
    },
)
async def get_audio(upload_id: str) -> FileResponse:
    """Cached MP3 transcode under ``ASR_DATA_DIR/cache/audio``; honours ``Range``."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
