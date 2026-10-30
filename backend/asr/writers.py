"""Transcript export: txt / srt / vtt / json / md.

Pure functions over the core ``Transcript``; no web imports (AGENTS.md rule 5). Speaker labels use
the display names from ``Transcript.speakers`` and fall back to the opaque speaker id. Markdown
(``asr.export.markdown``) also uses ``meta`` (title, source, date, model, summary) and ``locale``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from asr.providers.base import Segment, Transcript

if TYPE_CHECKING:
    from asr.export.markdown import ExportMeta, Locale

ExportFormat = Literal["txt", "srt", "vtt", "json", "md"]

MEDIA_TYPES: dict[str, str] = {
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "json": "application/json",
    "md": "text/markdown; charset=utf-8",
}

__all__ = ["MEDIA_TYPES", "ExportFormat", "merge_consecutive", "render", "write"]


# ---------------------------------------------------------------------------------- timestamps
def _clock_tenths(seconds: float) -> str:
    """``3.24 -> "00:00:03.2"`` (txt)."""
    # Via whole milliseconds, then half-up, so 3600.05 -> .1 (not banker's rounding of 36000.5).
    tenths = (round(max(seconds, 0.0) * 1000) + 50) // 100
    total_s, t = divmod(tenths, 10)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{t}"


def _clock_ms(seconds: float, sep: str) -> str:
    """``3.2 -> "00:00:03,200"`` (srt, ``sep=","``) or ``"00:00:03.200"`` (vtt)."""
    ms_total = round(max(seconds, 0.0) * 1000)
    total_s, ms = divmod(ms_total, 1000)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


# ------------------------------------------------------------------------------------- merging
def _join_text(left: str, right: str) -> str:
    """Join two segment texts; a space only between two Latin/digit boundaries."""
    left, right = left.rstrip(), right.lstrip()
    if not left or not right:
        return left + right
    a, b = left[-1], right[0]
    if a.isascii() and b.isascii() and a.isalnum() and b.isalnum():
        return f"{left} {right}"
    if a in ",.;:!?" and b.isascii() and b.isalnum():
        return f"{left} {right}"
    return left + right


def merge_consecutive(segments: list[Segment]) -> list[Segment]:
    """Join runs of consecutive segments with the same speaker into one segment."""
    merged: list[Segment] = []
    for seg in segments:
        if merged and merged[-1].speaker_id == seg.speaker_id:
            prev = merged[-1]
            merged[-1] = prev.model_copy(
                update={
                    "end": max(prev.end, seg.end),
                    "text": _join_text(prev.text, seg.text),
                    "words": [*prev.words, *seg.words],
                    "edited": prev.edited or seg.edited,
                }
            )
        else:
            merged.append(seg.model_copy())
    return merged


# --------------------------------------------------------------------------------------- render
def _vtt_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _one_line(text: str) -> str:
    """Subtitle cue text must not contain blank lines; collapse line breaks."""
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def write(
    transcript: Transcript,
    fmt: ExportFormat,
    *,
    timestamps: bool = True,
    merge_same_speaker: bool = False,
    locale: Locale = "zh",
    meta: ExportMeta | None = None,
) -> str:
    """Render ``transcript`` as ``fmt``.

    - ``txt``: ``[00:00:03.2 - 00:00:08.7] Name: text`` per segment (no prefix when
      ``timestamps=False``).
    - ``srt`` / ``vtt``: subtitle cues; timestamps are inherent, so ``timestamps`` is ignored.
    - ``json``: the transcript model as JSON (``ensure_ascii=False``).
    - ``md``: a Markdown note (``asr.export.markdown.render_markdown``); ``locale`` picks the
      heading language, ``meta`` the title / date / source / model / summary. Other formats
      ignore both.
    """
    if fmt == "md":
        from asr.export.markdown import render_markdown

        return render_markdown(
            transcript,
            meta,
            locale=locale,
            timestamps=timestamps,
            merge_same_speaker=merge_same_speaker,
        )

    segments = merge_consecutive(transcript.segments) if merge_same_speaker else transcript.segments
    names = {s.id: s.name for s in transcript.speakers}

    def name(seg: Segment) -> str:
        # A display name with line breaks would break the line / cue structure of every format.
        return _one_line(names.get(seg.speaker_id, seg.speaker_id))

    if fmt == "txt":
        lines = []
        for seg in segments:
            line = f"{name(seg)}: {seg.text.strip()}"
            if timestamps:
                line = f"[{_clock_tenths(seg.start)} - {_clock_tenths(seg.end)}] {line}"
            lines.append(line)
        return "\n".join(lines) + ("\n" if lines else "")

    if fmt == "srt":
        blocks = []
        for i, seg in enumerate(segments, start=1):
            span = f"{_clock_ms(seg.start, ',')} --> {_clock_ms(seg.end, ',')}"
            blocks.append(f"{i}\n{span}\n{name(seg)}: {_one_line(seg.text)}\n")
        return "\n".join(blocks)

    if fmt == "vtt":
        blocks = ["WEBVTT\n"]
        for seg in segments:
            span = f"{_clock_ms(seg.start, '.')} --> {_clock_ms(seg.end, '.')}"
            voice = _vtt_escape(name(seg))
            blocks.append(f"{span}\n<v {voice}>{_vtt_escape(_one_line(seg.text))}\n")
        return "\n".join(blocks)

    if fmt == "json":
        data = transcript.model_copy(update={"segments": segments}).model_dump(mode="json")
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    raise ValueError(f"unsupported export format: {fmt!r}")


# Name used by the Phase 1 stub.
render = write
