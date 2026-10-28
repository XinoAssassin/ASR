"""Uploads (PLAN §5.1). Owned by ``backend-core`` (M1)."""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Path, Request, UploadFile, status

from server.schemas import (
    UploadInitIn,
    UploadInitOut,
    UploadOut,
    UploadPartOut,
    UploadRegisterIn,
)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_OCTET_STREAM_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


@router.post("", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: Annotated[UploadFile, File(description="Audio/video file.")],
    target_dir: Annotated[str, Form(max_length=4096)],
) -> UploadOut:
    """Single-request multipart upload (curl; the UI uses chunked upload, D11)."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/init", response_model=UploadInitOut, status_code=status.HTTP_201_CREATED)
async def init_upload(body: UploadInitIn) -> UploadInitOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.put(
    "/{session_id}/parts/{n}",
    response_model=UploadPartOut,
    openapi_extra=_OCTET_STREAM_BODY,
)
async def upload_part(
    session_id: str, n: Annotated[int, Path(ge=0)], request: Request
) -> UploadPartOut:
    """Raw part body (``application/octet-stream``); read via ``request.stream()``."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post(
    "/{session_id}/complete", response_model=UploadOut, status_code=status.HTTP_201_CREATED
)
async def complete_upload(session_id: str) -> UploadOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/register", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def register_upload(body: UploadRegisterIn) -> UploadOut:
    """Register a file that already exists inside an upload root (no transfer)."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/{upload_id}", response_model=UploadOut)
async def get_upload(upload_id: str) -> UploadOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
