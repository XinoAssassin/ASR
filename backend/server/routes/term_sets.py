"""Term sets (PLAN §5.3). Owned by ``backend-jobs`` (M1)."""

from fastapi import APIRouter, HTTPException, Response, status

from server.schemas import TermSetImport, TermSetIn, TermSetOut

router = APIRouter(prefix="/api/term-sets", tags=["term-sets"])


@router.get("", response_model=list[TermSetOut])
async def list_term_sets() -> list[TermSetOut]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("", response_model=TermSetOut, status_code=status.HTTP_201_CREATED)
async def create_term_set(body: TermSetIn) -> TermSetOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/import", response_model=TermSetOut, status_code=status.HTTP_201_CREATED)
async def import_term_set(body: TermSetImport) -> TermSetOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/{term_set_id}", response_model=TermSetOut)
async def get_term_set(term_set_id: str) -> TermSetOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.put("/{term_set_id}", response_model=TermSetOut)
async def update_term_set(term_set_id: str, body: TermSetIn) -> TermSetOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.delete("/{term_set_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_term_set(term_set_id: str) -> Response:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
