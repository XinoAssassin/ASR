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

A corrected segment's ``words`` are re-tiled onto the new text: words untouched by an edit keep
their text and times, and the words an edit overlaps are merged into one word carrying the new
text of that stretch and their combined time span (a collapsed repetition becomes one long word, a
term replacement one word for the term). If the raw words cannot be mapped onto the raw text, the
corrected segment gets no words rather than stale ones.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from asr.providers.base import Correction, CorrectionOptions, Segment, Term, Word

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
        if text == seg.text:
            out.append(seg)
        else:
            words = retile_words(seg.text, text, seg.words, log.edits)
            out.append(seg.model_copy(update={"text": text, "words": words}))
    return out, corrections


def retile_words(old: str, new: str, words: Sequence[Word], edits: Sequence[_Edit]) -> list[Word]:
    """``words`` of ``old`` moved onto ``new`` (``edits`` turn one into the other, offsets in
    ``new``); see the module docstring. ``[]`` when the words do not map onto ``old``."""
    from asr.providers.local.merge import Token, token_spans

    if not words:
        return []
    spans = token_spans(old, [Token(w.text, w.start, w.end) for w in words])
    if spans is None:
        return []
    # Each edit in old- and new-text coordinates.
    regions: list[tuple[int, int, int, int]] = []
    shift = 0
    for e in sorted(edits, key=lambda e: e.offset):
        start = e.offset - shift
        regions.append((start, start + len(e.before), e.offset, e.offset + len(e.after)))
        shift += len(e.after) - len(e.before)

    def moved(pos: int, *, end: bool) -> int:
        """Position ``pos`` of ``old`` in ``new``; inside an edit: that edit's new edge."""
        delta = 0
        for os_, oe, ns, ne in regions:
            if pos >= oe:
                delta = ne - oe
            elif pos > os_:
                return ne if end else ns
            else:
                break
        return pos + delta

    out: list[Word] = []
    i = 0
    while i < len(words):
        s, e = spans[i]
        hit = [r for r in regions if r[0] < e and r[1] > s]
        if not hit:
            out.append(words[i])
            i += 1
            continue
        # Absorb following words while they overlap one of the edits gathered so far.
        j = i + 1
        while j < len(words):
            s2, e2 = spans[j]
            more = [r for r in regions if r[0] < e2 and r[1] > s2]
            if not any(r in hit for r in more):
                break
            hit += [r for r in more if r not in hit]
            e = max(e, e2)
            j += 1
        lo = min(moved(s, end=False), *(r[2] for r in hit))
        hi = max(moved(e, end=True), *(r[3] for r in hit))
        text = new[lo:hi].strip()
        if text:
            out.append(
                Word(
                    text=text,
                    start=min(w.start for w in words[i:j]),
                    end=max(w.end for w in words[i:j]),
                )
            )
        i = j
    if token_spans(new, [Token(w.text, w.start, w.end) for w in out]) is None:
        return []
    return out
