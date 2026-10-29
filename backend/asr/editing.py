"""Transcript editing operations (PLAN M7): pure functions over segments / speakers / corrections.

Used by ``PATCH /api/transcripts/{id}``; every function returns new lists and raises
``EditError`` (a user-facing message) instead of applying a partial change. ``original`` is never
touched (AGENTS rule 9). Timestamps of split segments come from word timestamps when they map onto
the text, otherwise they are interpolated by character position (PLAN §9).
"""

from __future__ import annotations

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

    # ----------------------------------------------------------------------------------- ops
    def edit_text(self, segment_id: str, text: str) -> None:
        i = self.index(segment_id)
        self.segments[i] = self._with_text(self.segments[i], text)

    def set_speaker(self, segment_id: str, speaker_id: str) -> None:
        if speaker_id not in self.speaker_ids():
            raise EditError(f"unknown speaker {speaker_id!r}")
        i = self.index(segment_id)
        self.segments[i] = self.segments[i].model_copy(update={"speaker_id": speaker_id})

    def split(self, segment_id: str, at: int) -> str:
        """Split before character ``at``; returns the id of the new second segment."""
        i = self.index(segment_id)
        seg = self.segments[i]
        left_text, right_text = seg.text[:at].rstrip(), seg.text[at:].lstrip()
        if not left_text or not right_text:
            raise EditError("split position must leave text on both sides")
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
        for k, c in enumerate(self.corrections):  # corrections in the tail follow the text
            if c.segment_id == seg.id and c.offset is not None and c.offset >= at:
                shift = len(seg.text) - len(right_text)
                self.corrections[k] = c.model_copy(
                    update={"segment_id": new_id, "offset": c.offset - shift}
                )
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

    def replace(self, find: str, replacement: str) -> int:
        """Literal find / replace in every segment; returns the number of replacements."""
        if not find:
            raise EditError("find text must not be empty")
        count = 0
        for i, seg in enumerate(self.segments):
            n = seg.text.count(find)
            if n:
                count += n
                self.segments[i] = self._with_text(seg, seg.text.replace(find, replacement))
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
        k = next((k for k, c in enumerate(self.corrections) if c.id == correction_id), None)
        if k is None:
            raise EditError(f"unknown correction {correction_id!r}")
        c = self.corrections[k]
        if c.status in ("accepted", "rejected"):
            raise EditError("this correction was already reviewed")
        try:
            i = self.index(c.segment_id)
        except EditError:
            i = None  # its segment is gone (merged away): review only changes the status
        if i is not None:
            seg = self.segments[i]
            if decision == "reject" and c.status == "applied":
                text = _swap(seg.text, c.after, c.before, c.offset)
                if text is not None:
                    self.segments[i] = self._with_text(seg, text)
            elif decision == "accept" and c.status == "suggested":
                text = _swap(seg.text, c.before, c.after, c.offset)
                if text is None:
                    raise EditError("the suggested text no longer matches the segment")
                self.segments[i] = self._with_text(seg, text)
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


def _swap(text: str, old: str, new: str, offset: int | None) -> str | None:
    """Replace ``old`` at ``offset`` (or its first occurrence); ``None`` if it is not there."""
    if offset is not None and text[offset : offset + len(old)] == old:
        return text[:offset] + new + text[offset + len(old) :]
    idx = text.find(old)
    if idx < 0:
        return None
    return text[:idx] + new + text[idx + len(old) :]


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
