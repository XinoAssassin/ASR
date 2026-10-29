"""Term sets (PLAN §5.3). Owned by ``backend-jobs`` (M1).

The term helpers (``parse_terms`` plain-text import, ``merge_terms`` dedupe) live in the core
library (``asr.terms``) and are re-exported here for job creation.
"""

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from asr.providers.base import Term
from asr.terms import merge_terms, parse_terms
from server.deps import DbDep
from server.schemas import TermSetImport, TermSetIn, TermSetOut

router = APIRouter(prefix="/api/term-sets", tags=["term-sets"])

MAX_TERMS_PER_SET = 1000


def _check_size(terms: list[Term]) -> list[Term]:
    terms = merge_terms(terms)
    if len(terms) > MAX_TERMS_PER_SET:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"a term set holds at most {MAX_TERMS_PER_SET} terms (got {len(terms)})",
        )
    return terms


def _name(name: str) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name must not be blank")
    return name


async def _get_or_404(db: DbDep, term_set_id: str) -> TermSetOut:
    term_set = await db.get_term_set(term_set_id)
    if term_set is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "term set not found")
    return term_set


@router.get("", response_model=list[TermSetOut])
async def list_term_sets(db: DbDep) -> list[TermSetOut]:
    return await db.list_term_sets()


@router.post("", response_model=TermSetOut, status_code=status.HTTP_201_CREATED)
async def create_term_set(body: TermSetIn, db: DbDep) -> TermSetOut:
    return await db.insert_term_set(uuid.uuid4().hex, _name(body.name), _check_size(body.terms))


@router.post("/import", response_model=TermSetOut, status_code=status.HTTP_201_CREATED)
async def import_term_set(body: TermSetImport, db: DbDep) -> TermSetOut:
    """Create a term set from pasted text / a .txt file's contents (see ``parse_terms``)."""
    terms, errors = parse_terms(body.text)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, errors)
    return await db.insert_term_set(uuid.uuid4().hex, _name(body.name), _check_size(terms))


@router.get("/{term_set_id}", response_model=TermSetOut)
async def get_term_set(term_set_id: str, db: DbDep) -> TermSetOut:
    return await _get_or_404(db, term_set_id)


@router.put("/{term_set_id}", response_model=TermSetOut)
async def update_term_set(term_set_id: str, body: TermSetIn, db: DbDep) -> TermSetOut:
    updated = await db.update_term_set(term_set_id, _name(body.name), _check_size(body.terms))
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "term set not found")
    return updated


@router.delete("/{term_set_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_term_set(term_set_id: str, db: DbDep) -> Response:
    if not await db.delete_term_set(term_set_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "term set not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
