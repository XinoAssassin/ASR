"""Term sets (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Also hosts the term helpers shared with job creation: ``parse_terms`` (plain-text import) and
``merge_terms`` (case-insensitive dedupe keeping the highest weight).
"""

import re
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import ValidationError

from asr.providers.base import Term
from server.deps import DbDep
from server.schemas import TermSetImport, TermSetIn, TermSetOut

router = APIRouter(prefix="/api/term-sets", tags=["term-sets"])

MAX_TERMS_PER_SET = 1000

_COMMA_SPLIT = re.compile(r"[,，]")


def merge_terms(terms: Iterable[Term]) -> list[Term]:
    """Dedupe case-insensitively, keeping first-seen order/spelling, the highest weight and the
    first non-empty note."""
    merged: dict[str, Term] = {}
    for term in terms:
        key = term.term.casefold()
        prev = merged.get(key)
        if prev is None:
            merged[key] = term.model_copy()
            continue
        update: dict[str, Any] = {}
        if term.weight > prev.weight:
            update["weight"] = term.weight
        if not prev.note and term.note:
            update["note"] = term.note
        for field in ("aliases", "blacklist"):
            extra = [w for w in getattr(term, field) if w not in getattr(prev, field)]
            if extra:
                update[field] = [*getattr(prev, field), *extra]
        if update:
            merged[key] = prev.model_copy(update=update)
    return list(merged.values())


def _line_error(line_no: int, line: str, msg: str) -> dict[str, Any]:
    # Same shape as FastAPI validation errors so the client handles both alike.
    return {
        "loc": ["body", "text", line_no],
        "msg": f"line {line_no}: {msg}",
        "type": "value_error",
        "input": line,
    }


def _split_hotword(term_part: str) -> tuple[str, list[str], list[str]]:
    """``Target | alias1 | alias2 ~~~ black1 | black2`` (asr-hotword syntax) -> parts."""
    left, _, right = term_part.partition("~~~")
    names = [p.strip() for p in left.split("|")]
    blacklist = [p.strip() for p in right.split("|")] if right else []
    return names[0], [n for n in names[1:] if n], [b for b in blacklist if b]


def parse_terms(text: str) -> tuple[list[Term], list[dict[str, Any]]]:
    """Parse one term per line: ``term``, ``term,weight``, ``term<TAB>weight<TAB>note``.

    The term part may use the asr-hotword syntax ``Target | alias | alias ~~~ black | black``
    (aliases = known mis-hearings for post-correction, blacklist = neighbouring words that block
    a replacement). Full-width ``，`` works like ``,``. Blank lines and ``#`` comments are skipped.
    Returns the terms (not deduplicated) and FastAPI-style errors with 1-based line numbers.
    """
    terms: list[Term] = []
    errors: list[dict[str, Any]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 2) if "\t" in line else _COMMA_SPLIT.split(line, maxsplit=2)
        parts = [p.strip() for p in parts]
        term_text, aliases, blacklist = _split_hotword(parts[0])
        weight_text = parts[1] if len(parts) > 1 else ""
        note = parts[2] if len(parts) > 2 and parts[2] else None
        data: dict[str, Any] = {
            "term": term_text,
            "note": note,
            "aliases": aliases,
            "blacklist": blacklist,
        }
        if weight_text:
            try:
                data["weight"] = int(weight_text)
            except ValueError:
                errors.append(_line_error(line_no, raw, f"weight {weight_text!r} is not a number"))
                continue
        try:
            terms.append(Term(**data))
        except ValidationError as exc:
            msg = "; ".join(e["msg"] for e in exc.errors(include_url=False, include_input=False))
            errors.append(_line_error(line_no, raw, msg))
    return terms, errors


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
