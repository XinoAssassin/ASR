"""Transcript editing operations (PLAN M7): pure functions over segments / speakers / corrections.

Used by ``PATCH /api/transcripts/{id}``; every function returns new lists and raises
``EditError`` (a user-facing message) instead of applying a partial change. ``original`` is never
touched (AGENTS rule 9). Timestamps of split segments come from word timestamps when they map onto
the text, otherwise they are interpolated by character position (PLAN §9).

Positions (``split(at)``, ``Correction.offset``) are **code-point** indices into the segment's
current text (Python ``str`` indices; the frontend converts from UTF-16). Every text mutation of a
segment keeps its corrections' offsets in step: corrections after the change move by the length
delta, corrections overlapping it lose their offset (``None``) and can then no longer be applied or
reverted, only dismissed. Review never applies a correction at a guessed location.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from asr.providers.base import SPEAKER_COLORS, Correction, Segment, Speaker, Word
from asr.providers.local.merge import Token, token_spans


class EditError(ValueError):
    """The edit cannot be applied; the message is shown to the user."""


@dataclass
class EditState:
    segments: list[Segment]
    speakers: list[Speaker]
    corrections: list[Correction]
    original_text: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------------------- helpers
    def index(self, segment_id: str) -> int:
        for i, s in enumerate(self.segments):
            if s.id == segment_id:
                return i
        raise EditError(f"unknown segment {segment_id!r}")

    def speaker_ids(self) -> set[str]:
        return {s.id for s in self.speakers}

    def _with_text(self, seg: Segment, text: str) -> Segment:
        return seg.model_copy(
            update={"text": text, "edited": text != self.original_text.get(seg.id)}
        )

    def _new_id(self, base: str) -> str:
        taken = {s.id for s in self.segments}
        n = 1
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def _mutate(self, i: int, start: int, end: int, new: str, *, keep: str | None = None) -> None:
        """Replace ``[start, end)`` of segment ``i`` with ``new`` and rebase correction offsets.

        ``keep`` is the id of a correction whose own span is the change (a review): its offset
        stays put.
        """
        seg = self.segments[i]
        if seg.text[start:end] == new:
            return
        self.segments[i] = self._with_text(seg, seg.text[:start] + new + seg.text[end:])
        delta = len(new) - (end - start)
        for k, c in enumerate(self.corrections):
            if c.segment_id != seg.id or c.offset is None or c.id == keep:
                continue
            if c.offset >= end:
                if delta:
                    self.corrections[k] = c.model_copy(update={"offset": c.offset + delta})
            elif c.offset + _span_len(c) > start:  # overlaps the change: position is lost
                self.corrections[k] = c.model_copy(update={"offset": None})

    # ----------------------------------------------------------------------------------- ops
    def edit_text(self, segment_id: str, text: str) -> None:
        i = self.index(segment_id)
        old = self.segments[i].text
        start, end, new = _changed_span(old, text)
        self._mutate(i, start, end, new)
        # The edited flag follows the whole text even when nothing changed.
        self.segments[i] = self._with_text(self.segments[i], text)

    def set_speaker(self, segment_id: str, speaker_id: str) -> None:
        if speaker_id not in self.speaker_ids():
            raise EditError(f"unknown speaker {speaker_id!r}")
        i = self.index(segment_id)
        self.segments[i] = self.segments[i].model_copy(update={"speaker_id": speaker_id})

    def split(self, segment_id: str, at: int) -> str:
        """Split before code point ``at`` of the text; returns the id of the new second segment.

        ``at`` is a code-point (Python ``str``) index, not UTF-16: the frontend converts. Whitespace
        around the split point is dropped. Corrections move with their text to the side they lie
        on (offsets rebased); one straddling the split point loses its offset.
        """
        i = self.index(segment_id)
        seg = self.segments[i]
        if not 0 < at < len(seg.text):
            raise EditError("split position must leave text on both sides")
        left_text, right_text = seg.text[:at].rstrip(), seg.text[at:].lstrip()
        if not left_text or not right_text:
            raise EditError("split position must leave text on both sides")
        right_start = len(seg.text) - len(right_text)
        split_time, left_words, right_words = _split_point(seg, at)
        new_id = self._new_id(seg.id)
        left = seg.model_copy(
            update={"text": left_text, "end": split_time, "words": left_words, "edited": True}
        )
        right = seg.model_copy(
            update={
                "id": new_id,
                "text": right_text,
                "start": split_time,
                "words": right_words,
                "edited": True,
            }
        )
        self.segments[i : i + 1] = [left, right]
        for k, c in enumerate(self.corrections):
            if c.segment_id != seg.id or c.offset is None:
                continue
            if c.offset >= right_start:
                update = {"segment_id": new_id, "offset": c.offset - right_start}
                self.corrections[k] = c.model_copy(update=update)
            elif c.offset + _span_len(c) > len(left_text):
                self.corrections[k] = c.model_copy(update={"offset": None})
        return new_id

    def merge(self, segment_id: str) -> None:
        """Join a segment with the next one in time order (the first one's speaker wins)."""
        order = sorted(range(len(self.segments)), key=lambda k: (self.segments[k].start, k))
        pos = order.index(self.index(segment_id))
        if pos + 1 >= len(order):
            raise EditError("the last segment has nothing to merge with")
        a, b = self.segments[order[pos]], self.segments[order[pos + 1]]
        sep = " " if _latin(a.text[-1:]) and _latin(b.text[:1]) else ""
        shift = len(a.text) + len(sep)
        merged = a.model_copy(
            update={
                "text": a.text + sep + b.text,
                "end": max(a.end, b.end),
                "words": [*a.words, *b.words],
                "edited": True,
            }
        )
        self.segments[order[pos]] = merged
        del self.segments[order[pos + 1]]
        for k, c in enumerate(self.corrections):
            if c.segment_id == b.id:
                offset = None if c.offset is None else c.offset + shift
                self.corrections[k] = c.model_copy(update={"segment_id": a.id, "offset": offset})

    def replace(self, find: str, replacement: str, *, ignore_case: bool = False) -> int:
        """Literal find / replace in every segment; returns the number of replacements.

        With ``ignore_case`` the literal ``find`` matches case-insensitively.
        """
        if not find:
            raise EditError("find text must not be empty")
        pattern = re.compile(re.escape(find), re.IGNORECASE if ignore_case else 0)
        count = 0
        for i, seg in enumerate(self.segments):
            spans = [m.span() for m in pattern.finditer(seg.text)]
            count += len(spans)
            for start, end in reversed(spans):  # right to left: earlier spans stay valid
                self._mutate(i, start, end, replacement)
        return count

    def merge_speakers(self, from_id: str, into_id: str) -> None:
        ids = self.speaker_ids()
        if from_id not in ids or into_id not in ids:
            raise EditError("both speakers must exist")
        if from_id == into_id:
            raise EditError("cannot merge a speaker into itself")
        self.segments = [
            s.model_copy(update={"speaker_id": into_id}) if s.speaker_id == from_id else s
            for s in self.segments
        ]
        self.speakers = [s for s in self.speakers if s.id != from_id]

    def review_correction(self, correction_id: str, decision: str) -> None:
        """Accept or reject a correction.

        Rejecting an ``applied`` one restores ``before`` and accepting a ``suggested`` one writes
        ``after``, strictly at ``offset``; if the text there is no longer the expected one the op
        fails. Accepting an applied / rejecting a suggested correction only changes its status.
        """
        k = next((k for k, c in enumerate(self.corrections) if c.id == correction_id), None)
        if k is None:
            raise EditError(f"unknown correction {correction_id!r}")
        c = self.corrections[k]
        if c.status in ("accepted", "rejected"):
            raise EditError("this correction was already reviewed")
        swap: tuple[str, str] | None = None
        if decision == "reject" and c.status == "applied":
            swap = (c.after, c.before)
        elif decision == "accept" and c.status == "suggested":
            swap = (c.before, c.after)
        if swap is not None:
            old, new = swap
            i = next((i for i, s in enumerate(self.segments) if s.id == c.segment_id), None)
            if (
                i is None
                or c.offset is None
                or self.segments[i].text[c.offset : c.offset + len(old)] != old
            ):
                raise EditError("correction no longer matches the text")
            self._mutate(i, c.offset, c.offset + len(old), new, keep=c.id)
        status = "accepted" if decision == "accept" else "rejected"
        self.corrections[k] = c.model_copy(update={"status": status})


def add_speakers(speakers: Sequence[Speaker], new_ids: Sequence[str]) -> list[Speaker]:
    out = list(speakers)
    for sid in new_ids:
        color = SPEAKER_COLORS[len(out) % len(SPEAKER_COLORS)]
        out.append(Speaker(id=sid, name=sid, color=color))
    return out


def _latin(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _span_len(c: Correction) -> int:
    """Length of the correction's text as it currently stands in the segment."""
    return len(c.after) if c.status in ("applied", "accepted") else len(c.before)


def _changed_span(old: str, new: str) -> tuple[int, int, str]:
    """``(start, end, replacement)`` turning ``old`` into ``new`` (common prefix / suffix)."""
    limit = min(len(old), len(new))
    p = 0
    while p < limit and old[p] == new[p]:
        p += 1
    s = 0
    while s < limit - p and old[-1 - s] == new[-1 - s]:
        s += 1
    return p, len(old) - s, new[p : len(new) - s]


def _split_point(seg: Segment, at: int) -> tuple[float, list[Word], list[Word]]:
    """Split time and word lists for a split before character ``at``."""
    tokens = [Token(w.text, w.start, w.end) for w in seg.words]
    spans = token_spans(seg.text, tokens) if tokens else None
    if spans:
        k = next((j for j, (start, _) in enumerate(spans) if start >= at), len(spans))
        if 0 < k < len(spans):
            when = seg.words[k].start
            return min(max(when, seg.start), seg.end), seg.words[:k], seg.words[k:]
    ratio = at / max(len(seg.text), 1)
    when = round(seg.start + (seg.end - seg.start) * ratio, 3)
    left = [w for w in seg.words if w.end <= when]
    right = [w for w in seg.words if w.end > when]
    return when, left, right
