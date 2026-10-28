"""Directory picker (PLAN §5.1). Owned by ``backend-core`` (M1)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from server.schemas import FsDirsOut, FsRoot

router = APIRouter(prefix="/api/fs", tags=["fs"])


@router.get("/roots", response_model=list[FsRoot])
async def list_roots() -> list[FsRoot]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/dirs", response_model=FsDirsOut)
async def list_dirs(
    path: Annotated[
        str | None, Query(max_length=4096, description="Defaults to the first root.")
    ] = None,
) -> FsDirsOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
