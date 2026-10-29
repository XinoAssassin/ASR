"""[5b] Post-correction of recognised text (docs/research/post-correction.md).

Runs after any provider, on the final segments:

- **repetition filter**: a 2-10 character unit repeated 5+ times in a row collapses to one copy, a
  single character repeated 8+ times to three (Qwen3-ASR issue #140 pattern; "好好好" and
  "哈哈哈" are left alone);
- **L1 term correction**: asr-hotword's phoneme matcher replaces spans that sound like a term or
  one of its aliases (pinyin initials / finals / tones for Chinese, letters for English), unless
  a blacklisted word is nearby.

Every edit becomes a ``Correction``; callers keep the uncorrected segments as ``original``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from asr.providers.base import Correction, CorrectionOptions, Segment, Term

_UNIT_REPEAT = re.compile(r"(.{2,10}?)\1{4,}")
_CHAR_REPEAT = re.compile(r"(.)\1{7,}")


def collapse_repetitions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return the collapsed text and the ``(before, after)`` pairs that changed."""
    edits: list[tuple[str, str]] = []

    def unit(m: re.Match[str]) -> str:
        edits.append((m.group(0), m.group(1)))
        return m.group(1)

    def char(m: re.Match[str]) -> str:
        edits.append((m.group(0), m.group(1) * 3))
        return m.group(1) * 3

    # Single characters first, so "哈" x 10 keeps three instead of becoming a collapsed "哈哈" unit.
    text = _CHAR_REPEAT.sub(char, text)
    text = _UNIT_REPEAT.sub(unit, text)
    return text, edits


def hotword_lines(terms: Sequence[Term]) -> str:
    """asr-hotword syntax: ``Target | alias | alias ~~~ black | black`` per line."""
    lines = []
    for t in terms:
        names = " | ".join([t.term, *t.aliases])
        lines.append(f"{names} ~~~ {' | '.join(t.blacklist)}" if t.blacklist else names)
    return "\n".join(lines)


class TermCorrector:
    def __init__(self, terms: Sequence[Term], *, threshold: float = 0.85) -> None:
        from hotword import PhonemeCorrector

        self._corrector = PhonemeCorrector(threshold=threshold)
        self.size = self._corrector.update_hotwords(hotword_lines(terms)) if terms else 0

    def correct(self, text: str) -> tuple[str, list[tuple[str, str, float]]]:
        if not self.size or not text.strip():
            return text, []
        result = self._corrector.correct(text)
        # A match may restate the target unchanged (e.g. case already right): not an edit.
        matches = [(a, b, float(s)) for a, b, s in result.matches if a != b]
        return result.text, matches


def correct_segments(
    segments: Sequence[Segment], terms: Sequence[Term], options: CorrectionOptions
) -> tuple[list[Segment], list[Correction]]:
    """Corrected copies of ``segments`` and the edits made (input segments are not modified)."""
    corrector = (
        TermCorrector(terms, threshold=options.threshold) if options.terms and terms else None
    )
    out: list[Segment] = []
    corrections: list[Correction] = []
    for seg in segments:
        text = seg.text
        if options.repetition:
            text, reps = collapse_repetitions(text)
            corrections += [
                Correction(segment_id=seg.id, source="repetition", before=b, after=a)
                for b, a in reps
            ]
        if corrector is not None:
            text, matches = corrector.correct(text)
            corrections += [
                Correction(segment_id=seg.id, source="term", before=b, after=a, score=round(s, 3))
                for b, a, s in matches
            ]
        out.append(seg if text == seg.text else seg.model_copy(update={"text": text}))
    return out, corrections
