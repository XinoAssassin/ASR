"""``GET /api/health`` (no auth)."""

import logging

from fastapi import APIRouter

from asr import __version__
from server.deps import EnvDep
from server.schemas import HealthOut

router = APIRouter(prefix="/api", tags=["health"])
log = logging.getLogger(__name__)


def _ffmpeg_version() -> str | None:
    try:
        from asr.audio.decode import ffmpeg_version

        return ffmpeg_version()
    except Exception:  # a broken native extension must not take the health check down
        log.warning("bundled FFmpeg unavailable", exc_info=True)
        return None


@router.get("/health", response_model=HealthOut)
async def health(env: EnvDep) -> HealthOut:
    return HealthOut(
        status="ok",
        version=__version__,
        api_key_required=env.api_key_required,
        decoder=env.decoder,
        ffmpeg_version=_ffmpeg_version(),
    )
