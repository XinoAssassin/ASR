"""[5b] Post-correction of recognised text (docs/research/post-correction.md).

Runs after any provider, on the final segments:

- **repetition filter**: a 2-10 character unit repeated 5+ times in a row collapses to one copy, a
  single character repeated 8+ times to three (Qwen3-ASR issue #140 pattern; "好好好" and
  "哈哈哈" are left alone). Digits never take part (``100000000`` and phone numbers are data), and
  a unit must start with a letter, so runs of punctuation / whitespace are left alone too;
- **L1 term correction**: asr-hotword's phoneme matcher replaces spans that sound like a term or
  one of its aliases (pinyin initials / finals / tones for Chinese, letters for English), unless
  a blacklisted word is nearby.

Every edit becomes a ``Correction`` whose ``offset`` is the code-point position of ``after`` in the
final corrected text (overlapping edits of the two passes are joined into one), so a review can
revert it exactly; callers keep the uncorrected segments as ``original``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from asr.providers.base import Correction, CorrectionOptions, Segment, Term

# A letter (any script) then up to 9 non-digit characters; never digits, never a newline.
_UNIT_REPEAT = re.compile(r"([^\W\d_][^\d\n]{1,9}?)\1{4,}")
_CHAR_REPEAT = re.compile(r"([^\W\d_])\1{7,}")


@dataclass
class _Edit:
    offset: int  # position of ``after`` in the current text
    before: str
    after: str
    source: str = "repetition"
    score: float | None = None


class _EditLog:
    """Text plus the edits made to it so far, kept in current-text coordinates."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[_Edit] = []

    def replace(
        self, start: int, end: int, new: str, source: str, score: float | None = None
    ) -> None:
        """Replace ``[start, end)`` of the current text; overlapping earlier edits are joined."""
        hit = [e for e in self.edits if e.offset < end and e.offset + len(e.after) > start]
        lo = min([start, *(e.offset for e in hit)])
        hi = max([end, *(e.offset + len(e.after) for e in hit)])
        parts, pos = [], lo
        for e in sorted(hit, key=lambda e: e.offset):
            parts += [self.text[pos : e.offset], e.before]
            pos = e.offset + len(e.after)
        parts.append(self.text[pos:hi])
        before = "".join(parts)
        after = self.text[lo:start] + new + self.text[end:hi]
        delta = len(new) - (end - start)
        self.text = self.text[:start] + new + self.text[end:]
        for e in self.edits:
            if e not in hit and e.offset >= end:
                e.offset += delta
        if hit:
            first = hit[0]
            self.edits = [e for e in self.edits if e is first or e not in hit]
            first.offset, first.before, first.after = lo, before, after
            if before == after:
                self.edits.remove(first)
        elif before != after:
            self.edits.append(_Edit(start, before, after, source, score))


def _collapse(log: _EditLog) -> None:
    # Single characters first, so "哈" x 10 keeps three instead of becoming a collapsed "哈哈" unit.
    for m in reversed(list(_CHAR_REPEAT.finditer(log.text))):
        log.replace(m.start(), m.end(), m.group(1) * 3, "repetition")
    for m in reversed(list(_UNIT_REPEAT.finditer(log.text))):
        log.replace(m.start(), m.end(), m.group(1), "repetition")


def collapse_repetitions(text: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Return the collapsed text and the ``(offset, before, after)`` edits, ``offset`` being the
    position of ``after`` in the collapsed text."""
    log = _EditLog(text)
    _collapse(log)
    edits = sorted(log.edits, key=lambda e: e.offset)
    return log.text, [(e.offset, e.before, e.after) for e in edits]


def hotword_lines(terms: Sequence[Term]) -> str:
    """asr-hotword syntax: ``Target | alias | alias ~~~ black | black`` per line."""
    lines = []
    for t in terms:
        names = " | ".join([t.term, *t.aliases])
        lines.append(f"{names} ~~~ {' | '.join(t.blacklist)}" if t.blacklist else names)
    return "\n".join(lines)


def _locate(old: str, new: str, pairs: Sequence[tuple[str, str]]) -> list[int] | None:
    """Positions in ``old`` of the left-to-right, non-overlapping replacements ``pairs``
    (``(before, after)``) that turn ``old`` into ``new``; ``None`` if they do not."""

    def go(k: int, i: int, j: int) -> list[int] | None:
        if k == len(pairs):
            return [] if old[i:] == new[j:] else None
        before, after = pairs[k]
        p = old.find(before, i)
        while p >= 0:
            gap = p - i
            if new[j : j + gap] == old[i:p] and new[j + gap : j + gap + len(after)] == after:
                rest = go(k + 1, p + len(before), j + gap + len(after))
                if rest is not None:
                    return [p, *rest]
            p = old.find(before, p + 1)
        return None

    return go(0, 0, 0)


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

    def spans(self, text: str) -> list[tuple[int, int, str, float]]:
        """``(start, end, replacement, score)`` per replacement in ``text``, left to right.

        Empty when the matcher's output cannot be mapped back onto ``text`` (never guessed).
        """
        new, matches = self.correct(text)
        if not matches:
            return []
        ordered = matches[::-1]  # asr-hotword reports its non-overlapping matches right to left
        positions = _locate(text, new, [(a, b) for a, b, _ in ordered])
        if positions is None:
            return []
        return [(p, p + len(a), b, s) for p, (a, b, s) in zip(positions, ordered, strict=True)]


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
        log = _EditLog(seg.text)
        if options.repetition:
            _collapse(log)
        if corrector is not None:
            for start, end, new, score in reversed(corrector.spans(log.text)):
                log.replace(start, end, new, "term", round(score, 3))
        corrections += [
            Correction(
                segment_id=seg.id,
                source=e.source,  # type: ignore[arg-type]
                before=e.before,
                after=e.after,
                score=e.score,
                offset=e.offset,
            )
            for e in sorted(log.edits, key=lambda e: e.offset)
        ]
        text = log.text
        out.append(seg if text == seg.text else seg.model_copy(update={"text": text}))
    return out, corrections
