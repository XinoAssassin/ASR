"""Markdown transcript export (``format=md``) and the SiYuan-flavoured variant used by the push.

Layout (``flavor="md"``, see ``docs/research/siyuan-export.md`` for the SiYuan differences)::

    ---                      YAML front matter, every string double-quoted (JSON escapes)
    title: "..."
    ...
    ---

    # <title>

    > 2026-10-29 14:00 · 时长 33:52 · 4 位发言人 · 本地识别（qwen3-asr-1.7b）

    ## 摘要                  only with a summary: overview, ### 要点 / 决定 / 待办 (task list)
    ## 发言人                table: name | turns | talk time | share
    ## 文字稿                one paragraph per segment (or per turn with merge_same_speaker):
    **张三** `00:03:12`\\    bold name, start time as inline code, hard line break,
    text of the turn         then the escaped text on one line
    ## 备注                  only with transcript warnings

``flavor="siyuan"`` (``/api/filetree/createDocWithMd``): no front matter and no H1 (SiYuan
parses neither; the title is the document path and the metadata go to ``custom-asr-*``
document attributes, see ``siyuan_doc_attrs``), a plain line break inside each turn (SiYuan keeps
it as a line break), and a kramdown block IAL after every turn carrying the speaker and times
(``{: custom-asr-speaker="..." custom-asr-start="..."}``), which SiYuan stores as block attributes.

Transcript text is escaped so that it can never create Markdown structure: inline syntax
(emphasis, code, links, HTML, math, tags, highlight, sub/superscript, IAL, block refs, table
pipes) is backslash-escaped everywhere and block syntax (lists, quotes, setext underlines,
ordered-list numbers) at the start of a line. Ordinary CJK and Latin text is left alone, and so
is an underscore inside a word (``SPEAKER_00``), which CommonMark never treats as emphasis.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from asr.providers.base import Segment, Transcript
from asr.summarize import TranscriptSummary

Locale = Literal["zh", "en"]
Flavor = Literal["md", "siyuan"]

__all__ = [
    "ExportMeta",
    "Flavor",
    "Locale",
    "escape_inline",
    "escape_line",
    "render_markdown",
    "siyuan_doc_attrs",
]


@dataclass(frozen=True, slots=True)
class ExportMeta:
    """What the caller knows about a transcript beyond the core ``Transcript``.

    ``created_at`` is rendered in its own timezone (the server passes local time). ``tags``
    ``None`` means the locale default (``会议记录`` / ``meeting-notes``).
    """

    title: str | None = None
    source_name: str | None = None
    created_at: datetime | None = None
    model: str | None = None
    transcript_id: str | None = None
    summary: TranscriptSummary | None = None
    summary_stale: bool = False
    tags: tuple[str, ...] | None = None


_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "default_title": "会议记录 {date}",
        "untitled": "会议记录",
        "tag": "会议记录",
        "duration": "时长 {d}",
        "speakers_n": "{n} 位发言人",
        "local": "本地识别",
        "cloud": "云端识别",
        "with_model": "{p}（{m}）",
        "summary": "摘要",
        "key_points": "要点",
        "decisions": "决定",
        "action_items": "待办",
        "owner": "负责人：{x}",
        "due": "期限：{x}",
        "item_meta": "（{x}）",
        "summary_note": "由 {model} 自动生成，仅供参考。",
        "summary_stale": "摘要生成后文字稿有修改，内容可能已过时。",
        "sentence_sep": "",
        "speakers": "发言人",
        "col_speaker": "发言人",
        "col_turns": "发言次数",
        "col_time": "时长",
        "col_share": "占比",
        "transcript": "文字稿",
        "empty": "（无内容）",
        "notes": "备注",
    },
    "en": {
        "default_title": "Meeting {date}",
        "untitled": "Meeting",
        "tag": "meeting-notes",
        "duration": "duration {d}",
        "speakers_n": "{n} speakers",
        "local": "local recognition",
        "cloud": "cloud recognition",
        "with_model": "{p} ({m})",
        "summary": "Summary",
        "key_points": "Key points",
        "decisions": "Decisions",
        "action_items": "Action items",
        "owner": "owner: {x}",
        "due": "due: {x}",
        "item_meta": " ({x})",
        "summary_note": "Generated automatically by {model}; it may contain mistakes.",
        "summary_stale": "The transcript was edited after this summary was written, "
        "so it may be out of date.",
        "sentence_sep": " ",
        "speakers": "Speakers",
        "col_speaker": "Speaker",
        "col_turns": "Turns",
        "col_time": "Talk time",
        "col_share": "Share",
        "transcript": "Transcript",
        "empty": "(empty)",
        "notes": "Notes",
    },
}


# ------------------------------------------------------------------------------------ escaping
# Escaped wherever they appear. `#` (Obsidian / SiYuan tags), `$` (math on GitHub, Obsidian and
# SiYuan), `~` and `^` (strikethrough, SiYuan sub/superscript), `{` (kramdown IAL, SiYuan embeds)
# and `|` (table cells) included; `]`, `)` and `>` are harmless once their openers are escaped.
_ALWAYS = frozenset("\\`*[<#|~$^{")
_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_ORDERED = re.compile(r"[0-9]{1,9}(?=[.)])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def one_line(text: str) -> str:
    """Collapse line breaks (incl. U+2028/9, NEL) into spaces and drop control characters."""
    joined = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return _CONTROL.sub("", joined.replace("\t", " "))


def escape_inline(text: str) -> str:
    """Backslash-escape inline Markdown syntax in ``text`` (one line)."""
    out: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in _ALWAYS:
            out.append("\\" + ch)
        elif ch == "_":
            # Intraword underscores never open or close emphasis in CommonMark.
            out.append("_" if prev.isalnum() and nxt.isalnum() else "\\_")
        elif ch == "=" and "=" in (prev, nxt):
            out.append("\\=")  # ==highlight== (Obsidian, SiYuan)
        elif ch == "(" and nxt == "(":
            out.append("\\(")  # ((block ref)) (SiYuan)
        elif ch == "&" and _ENTITY.match(text, i):
            out.append("\\&")
        else:
            out.append(ch)
    return "".join(out)


def escape_line(text: str) -> str:
    """``escape_inline`` plus the block syntax that only counts at the start of a line (list
    markers, block quote, setext underline, ``1.`` / ``1)``). Surrounding blanks are dropped."""
    esc = escape_inline(text.strip())
    if m := _ORDERED.match(esc):
        return esc[: m.end()] + "\\" + esc[m.end() :]
    if esc[:1] in ("-", "+", ">", "="):
        return "\\" + esc
    return esc


def _yaml_str(value: str) -> str:
    """A YAML double-quoted scalar: JSON string escapes, plus the characters YAML treats as
    line breaks or forbids (C1 controls, U+2028/9, BOM)."""
    s = json.dumps(value, ensure_ascii=False)
    return re.sub(r"[\x7f-\x9f  ﻿]", lambda m: f"\\u{ord(m.group()):04x}", s)


def _ial_value(value: str) -> str:
    return html.escape(one_line(value), quote=True)


# ------------------------------------------------------------------------------------- numbers
def _hms(seconds: float) -> str:
    """``192.7 -> "00:03:12"`` (turn starts round down, so seeking lands before the speech)."""
    total = max(int(seconds), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _span(seconds: float) -> str:
    """Durations, rounded half up: ``"33:52"``, ``"1:02:05"``."""
    total = int(max(seconds, 0.0) + 0.5)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _span_hms(seconds: float) -> str:
    total = int(max(seconds, 0.0) + 0.5)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------------------- model
@dataclass(frozen=True, slots=True)
class _SpeakerStat:
    speaker_id: str
    name: str
    turns: int
    seconds: float


def _names(transcript: Transcript) -> dict[str, str]:
    return {s.id: one_line(s.name) for s in transcript.speakers}


def _speaker_name(names: dict[str, str], speaker_id: str) -> str:
    return names.get(speaker_id) or one_line(speaker_id) or speaker_id


def _speaker_stats(transcript: Transcript) -> list[_SpeakerStat]:
    """Speakers with speech, by talk time (ties: first appearance). A turn is a run of
    consecutive segments of one speaker, whatever ``merge_same_speaker`` says."""
    names = _names(transcript)
    turns: dict[str, int] = {}
    seconds: dict[str, float] = {}
    prev: str | None = None
    for seg in transcript.segments:
        sid = seg.speaker_id
        if sid != prev:
            turns[sid] = turns.get(sid, 0) + 1
        seconds[sid] = seconds.get(sid, 0.0) + max(seg.end - seg.start, 0.0)
        prev = sid
    stats = [
        _SpeakerStat(sid, _speaker_name(names, sid), turns[sid], seconds[sid]) for sid in turns
    ]
    return sorted(stats, key=lambda s: -s.seconds)


def _duration(transcript: Transcript) -> float | None:
    if transcript.duration is not None:
        return transcript.duration
    return max((s.end for s in transcript.segments), default=None)


def _title(meta: ExportMeta, labels: dict[str, str]) -> str:
    title = one_line(meta.title or "")
    if title:
        return title
    if meta.created_at is not None:
        return labels["default_title"].format(date=meta.created_at.strftime("%Y-%m-%d"))
    return labels["untitled"]


def _tags(meta: ExportMeta, labels: dict[str, str]) -> list[str]:
    raw = meta.tags if meta.tags is not None else (labels["tag"],)
    return [t for t in (one_line(t) for t in raw) if t]


def _provider_label(transcript: Transcript, meta: ExportMeta, labels: dict[str, str]) -> str:
    provider = labels.get(transcript.provider, transcript.provider)
    model = one_line(meta.model or "")
    return labels["with_model"].format(p=provider, m=model) if model else provider


# -------------------------------------------------------------------------------------- render
def _front_matter(transcript: Transcript, meta: ExportMeta, labels: dict[str, str]) -> str:
    fields: list[tuple[str, str]] = [("title", _yaml_str(_title(meta, labels)))]
    if meta.created_at is not None:
        fields.append(("date", meta.created_at.replace(microsecond=0).isoformat()))
    duration = _duration(transcript)
    if duration is not None:
        fields.append(("duration", _yaml_str(_span_hms(duration))))
    if meta.source_name:
        fields.append(("source", _yaml_str(one_line(meta.source_name))))
    fields.append(("provider", _yaml_str(transcript.provider)))
    if meta.model:
        fields.append(("model", _yaml_str(one_line(meta.model))))
    if transcript.language:
        fields.append(("language", _yaml_str(one_line(transcript.language))))
    speakers = [s.name for s in sorted(_speaker_stats(transcript), key=_first_seen(transcript))]
    fields.append(("speakers", "[" + ", ".join(_yaml_str(n) for n in speakers) + "]"))
    if meta.transcript_id:
        fields.append(("transcript_id", _yaml_str(meta.transcript_id)))
    fields.append(("tags", "[" + ", ".join(_yaml_str(t) for t in _tags(meta, labels)) + "]"))
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields) + "---"


def _first_seen(transcript: Transcript) -> Callable[[_SpeakerStat], int]:
    order: dict[str, int] = {}
    for seg in transcript.segments:
        order.setdefault(seg.speaker_id, len(order))
    return lambda stat: order[stat.speaker_id]


def _meta_line(transcript: Transcript, meta: ExportMeta, labels: dict[str, str]) -> str:
    parts: list[str] = []
    if meta.created_at is not None:
        parts.append(meta.created_at.strftime("%Y-%m-%d %H:%M"))
    duration = _duration(transcript)
    if duration is not None:
        parts.append(labels["duration"].format(d=_span(duration)))
    parts.append(labels["speakers_n"].format(n=len(_speaker_stats(transcript))))
    parts.append(_provider_label(transcript, meta, labels))
    return "> " + escape_inline(" · ".join(parts))


def _summary_blocks(meta: ExportMeta, labels: dict[str, str]) -> list[str]:
    summary = meta.summary
    if summary is None:
        return []
    blocks = [f"## {labels['summary']}"]
    note = labels["summary_note"].format(model=one_line(summary.model) or "LLM")
    if meta.summary_stale:
        note += labels["sentence_sep"] + labels["summary_stale"]
    blocks.append("> " + escape_inline(note))
    blocks += [escape_line(p) for p in summary.overview.splitlines() if p.strip()]

    def bullets(items: Sequence[str]) -> str:
        return "\n".join(f"- {escape_line(one_line(i))}" for i in items if one_line(i))

    for key, items in (("key_points", summary.key_points), ("decisions", summary.decisions)):
        if text := bullets(items):
            blocks += [f"### {labels[key]}", text]
    tasks: list[str] = []
    for item in summary.action_items:
        task = escape_line(one_line(item.task))
        if not task:
            continue
        extra = [
            labels[k].format(x=escape_inline(one_line(v)))
            for k, v in (("owner", item.owner), ("due", item.due))
            if v and one_line(v)
        ]
        suffix = labels["item_meta"].format(x=" · ".join(extra)) if extra else ""
        tasks.append(f"- [ ] {task}{suffix}")
    if tasks:
        blocks += [f"### {labels['action_items']}", "\n".join(tasks)]
    return blocks


def _speakers_table(transcript: Transcript, labels: dict[str, str]) -> list[str]:
    stats = _speaker_stats(transcript)
    if not stats:
        return []
    total = sum(s.seconds for s in stats)
    head = (
        f"| {labels['col_speaker']} | {labels['col_turns']} | {labels['col_time']} "
        f"| {labels['col_share']} |\n| --- | ---: | ---: | ---: |"
    )
    rows = []
    for s in stats:
        share = f"{int(100 * s.seconds / total + 0.5)}%" if total > 0 else "-"
        rows.append(f"| {escape_inline(s.name)} | {s.turns} | {_span(s.seconds)} | {share} |")
    return [f"## {labels['speakers']}", head + "\n" + "\n".join(rows)]


def _turn(seg: Segment, name: str, *, flavor: Flavor, timestamps: bool) -> str:
    head = f"**{escape_inline(name)}**"
    if timestamps:
        head += f" `{_hms(seg.start)}`"
    text = escape_line(one_line(seg.text))
    if flavor == "md":
        # Backslash hard break: renders on GitHub, Obsidian and Typora alike, and survives
        # editors that strip trailing spaces.
        return f"{head}\\\n{text}" if text else head
    ial = (
        f'{{: custom-asr-speaker="{_ial_value(name)}" '
        f'custom-asr-speaker-id="{_ial_value(seg.speaker_id)}" '
        f'custom-asr-start="{seg.start:.2f}" custom-asr-end="{seg.end:.2f}"}}'
    )
    return f"{head}\n{text}\n{ial}" if text else f"{head}\n{ial}"


def render_markdown(
    transcript: Transcript,
    meta: ExportMeta | None = None,
    *,
    flavor: Flavor = "md",
    locale: Locale = "zh",
    timestamps: bool = True,
    merge_same_speaker: bool = False,
) -> str:
    """Render ``transcript`` as a Markdown note. Deterministic for equal inputs."""
    from asr.writers import merge_consecutive  # writers imports this module lazily

    meta = meta or ExportMeta()
    labels = _LABELS[locale]
    blocks: list[str] = []
    if flavor == "md":
        blocks.append(_front_matter(transcript, meta, labels))
        blocks.append(f"# {escape_inline(_title(meta, labels))}")
    blocks.append(_meta_line(transcript, meta, labels))
    blocks += _summary_blocks(meta, labels)
    blocks += _speakers_table(transcript, labels)

    blocks.append(f"## {labels['transcript']}")
    segments = merge_consecutive(transcript.segments) if merge_same_speaker else transcript.segments
    names = _names(transcript)
    turns = [
        _turn(seg, _speaker_name(names, seg.speaker_id), flavor=flavor, timestamps=timestamps)
        for seg in segments
    ]
    blocks += turns or [labels["empty"]]

    warnings = [w for w in (one_line(w) for w in transcript.warnings) if w]
    if warnings:
        blocks.append(f"## {labels['notes']}")
        blocks.append("\n".join(f"- {escape_line(w)}" for w in warnings))
    return "\n\n".join(blocks) + "\n"


def siyuan_doc_attrs(
    transcript: Transcript, meta: ExportMeta | None = None, *, locale: Locale = "zh"
) -> dict[str, str]:
    """The front-matter fields as SiYuan document attributes (``custom-asr-*``), for
    ``/api/attr/setBlockAttrs``. Empty values are left out."""
    meta = meta or ExportMeta()
    labels = _LABELS[locale]
    duration = _duration(transcript)
    speakers = [s.name for s in sorted(_speaker_stats(transcript), key=_first_seen(transcript))]
    attrs = {
        "custom-asr-title": _title(meta, labels),
        "custom-asr-date": (
            meta.created_at.replace(microsecond=0).isoformat() if meta.created_at else ""
        ),
        "custom-asr-duration": _span_hms(duration) if duration is not None else "",
        "custom-asr-source": one_line(meta.source_name or ""),
        "custom-asr-provider": transcript.provider,
        "custom-asr-model": one_line(meta.model or ""),
        "custom-asr-language": one_line(transcript.language or ""),
        "custom-asr-speakers": ", ".join(speakers),
        "custom-asr-transcript-id": meta.transcript_id or "",
    }
    return {k: v for k, v in attrs.items() if v}


def doc_tags(meta: ExportMeta | None = None, *, locale: Locale = "zh") -> list[str]:
    """Document tags (``tags`` of ``createDocWithMd``)."""
    return _tags(meta or ExportMeta(), _LABELS[locale])
