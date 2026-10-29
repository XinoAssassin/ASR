"""Term list helpers shared by the CLI and the server: plain-text import and dedupe."""

import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from asr.providers.base import Term

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
