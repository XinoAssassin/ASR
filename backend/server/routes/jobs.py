"""Jobs (PLAN §5.2). Owned by ``backend-jobs`` (M1)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from server.schemas import JobCreate, JobList, JobOut, JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreate) -> JobOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get("", response_model=JobList)
async def list_jobs(
    status_: Annotated[JobStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JobList:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str) -> JobOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get(
    "/{job_id}/events",
    response_class=Response,
    responses={
        200: {
            "description": (
                "Server-sent events: `event: job`, `data: JobOut` (JSON) on every change; "
                "the stream ends after a terminal status."
            ),
            "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/JobOut"}}},
        }
    },
)
async def job_events(job_id: str) -> Response:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str) -> JobOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str) -> JobOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_job(
    job_id: str,
    delete_audio: Annotated[bool, Query(description="Also delete the uploaded file.")] = False,
) -> Response:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
