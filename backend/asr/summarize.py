"""LLM meeting summary (M11): overview, key points, decisions and action items.

Opt-in: the transcript text (speaker display names, segment text, coarse timestamps) is sent to
the configured chat model through the same transport as the [5b] L3 correction pass
(``asr.correct.llm``): DashScope (paid; key and allow-listed endpoint of the cloud ASR) or an
OpenAI-compatible server of the operator's choice. Short transcripts take one call. Longer ones
are cut into chunks of about ``CHUNK_CHARS`` characters at speaker-turn boundaries, each chunk is
summarised ("map") and the partial summaries are merged by one more call ("reduce").

Action-item owners are filled only when the transcript says who will do it; the model is told to
use the speaker's display name or the name mentioned in the text, never to guess.

Citations: every transcript line sent to the model starts with a short reference,
``[#12 03:41] 张三: ...`` (numbered from 1 across the whole transcript, so the numbers stay the same
in every chunk). The model returns, for each key point, decision and action item, the references
of 1-3 supporting lines (``"refs": ["#12"]``). Unknown references are dropped and the rest are
mapped back to the id of the line's first segment. In the chunked path a chunk may only cite its
own lines, the partial summaries go to the merge call with their ``#n`` references, and the merge
answer may only cite references that occur in the partials.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

import httpx
from pydantic import BaseModel, Field, model_validator

from asr.correct.llm import (
    DEFAULT_LLM_MODEL,
    LlmProviderName,
    chat_json_answer,
    chat_url,
    glossary,
    llm_client,
)
from asr.providers.base import ProviderError, Segment, Speaker, Term

CHUNK_CHARS = 12_000
MAX_TRANSCRIPT_CHARS = 400_000  # ~ 12 h of speech; beyond that the reduce prompt gets too big
MAX_ITEMS = 30
MAX_ITEM_CHARS = 500
MAX_OVERVIEW_CHARS = 3000
MAX_REFS = 3
LINE_CHARS = 300  # a long monologue is cut into several lines so citations stay precise

_REFS_DESCRIPTION = (
    "Ids of the segments the item is based on (at most 3), in transcript order: the first is "
    "the earliest supporting line. Empty for summaries written before citations existed, and "
    "an id may no longer exist after edits (merge) - clients skip those."
)


class SummaryPoint(BaseModel):
    """A key point or decision with the segments it cites.

    Summaries stored before citations existed hold plain strings; they load as
    ``{"text": "...", "refs": []}``.
    """

    text: str
    refs: list[str] = Field(default_factory=list, description=_REFS_DESCRIPTION)

    @model_validator(mode="before")
    @classmethod
    def _from_plain_string(cls, data: Any) -> Any:
        return {"text": data} if isinstance(data, str) else data


class ActionItem(BaseModel):
    task: str
    owner: str | None = Field(
        default=None, description="Speaker name or person named in the meeting, when stated."
    )
    due: str | None = Field(default=None, description="Deadline as said in the meeting.")
    refs: list[str] = Field(default_factory=list, description=_REFS_DESCRIPTION)


class TranscriptSummary(BaseModel):
    """LLM meeting summary.

    JSON shape::

        {"overview": "...",
         "key_points": [{"text": "...", "refs": ["seg_00012"]}],
         "decisions": [{"text": "...", "refs": ["seg_00030", "seg_00031"]}],
         "action_items": [{"task": "...", "owner": "张三" | null, "due": "周五" | null,
                           "refs": ["seg_00041"]}],
         "model": "qwen-plus", "chunks": 1, "created_at": "2026-11-05T10:00:00Z"}

    ``refs`` are segment ids (see ``SummaryPoint``). Older stored summaries with
    ``key_points`` / ``decisions`` as lists of strings and action items without ``refs`` are
    still accepted and come out in the shape above with empty ``refs``.
    """

    overview: str
    key_points: list[SummaryPoint] = Field(default_factory=list)
    decisions: list[SummaryPoint] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    model: str = Field(description="Chat model that wrote the summary.")
    chunks: int = Field(default=1, ge=1, description="Transcript chunks summarised (map step).")
    created_at: datetime


SCHEMA_HINT = (
    '{"overview": "...", "key_points": [{"text": "...", "refs": ["#12"]}], '
    '"decisions": [{"text": "...", "refs": ["#30", "#31"]}], '
    '"action_items": [{"task": "...", "owner": "说话人名称或 null", "due": "期限或 null", '
    '"refs": ["#41"]}]}'
)

SYSTEM_PROMPT = f"""你是会议纪要助手。输入是带说话人和时间的语音转写（可能有少量识别错误）。
请写出结构化摘要，只输出 JSON：{SCHEMA_HINT}
要求：
- overview：3-6 句话概括会议/对话的主题和结论；
- key_points：主要讨论点，每条一句话，按出现顺序，最多 12 条；
- decisions：明确达成的决定或结论，没有就给空列表；
- action_items：明确提到的后续行动。只有原文明确说明由谁负责时才填写 owner，
  使用说话人名称或原文提到的人名，否则为 null；
  due 同理，没有明确期限为 null；
- refs：转写每行开头 [#编号 时间] 里的 #编号 是这一行的引用编号。key_points、decisions、
  action_items 的每一条都用 refs 给出最能支持它的 1-3 行的编号（如 "#12"），
  只能使用输入里出现过的编号，不要写时间；
- 只依据原文，不编造，不补充原文没有的信息；术语按术语表写法，但不要在输出里提到术语表；
- 使用转写内容的主要语言输出（中文为主则用简体中文）。"""

MERGE_PROMPT = f"""你是会议纪要助手。输入是同一场会议按时间顺序分段写出的若干部分摘要（JSON）。
请合并为一份完整摘要，只输出 JSON：{SCHEMA_HINT}
要求：去掉重复，保留时间顺序；overview 概括整场会议（3-6 句）；key_points 最多 12 条；
decisions 和 action_items 全部保留（合并重复项），owner / due 只沿用部分摘要里已有的值，不要新增；
每一条都保留 refs：沿用部分摘要里对应条目的编号（如 "#12"），合并重复项时合并它们的 refs，
每条最多 3 个，不要新增部分摘要里没有的编号；不编造；使用部分摘要的语言。"""


def _clock(seconds: float) -> str:
    total = max(int(seconds), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class SourceLine(NamedTuple):
    """One numbered transcript line as sent to the model."""

    ref: str  # "12", shown to the model as "#12"
    segment_id: str  # first segment of the line: what a citation of the line points at
    text: str  # "[#12 03:41] 张三: ..."


def transcript_lines(segments: Sequence[Segment], speakers: Sequence[Speaker]) -> list[SourceLine]:
    """``[#n mm:ss] Name: text`` per speaker turn (consecutive segments of a speaker are joined;
    a turn longer than ``LINE_CHARS`` continues on a new line at a segment boundary)."""
    names = {s.id: s.name for s in speakers}
    lines: list[SourceLine] = []
    current: str | None = None
    first_id = ""
    start = 0.0
    parts: list[str] = []
    size = 0

    def flush() -> None:
        ref = str(len(lines) + 1)
        name = names.get(current or "", current)
        head = f"[#{ref} {_clock(start)}] {name}: "
        lines.append(SourceLine(ref, first_id, head + "".join(parts)))

    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        text = " ".join(seg.text.split())  # one line per entry, whatever the text holds
        if not text:
            continue
        if parts and (seg.speaker_id != current or size >= LINE_CHARS):
            flush()
            parts, size = [], 0
        if not parts:
            current, start, first_id = seg.speaker_id, seg.start, seg.id
        piece = text if not parts or not _needs_space(parts[-1], text) else " " + text
        parts.append(piece)
        size += len(piece)
    if parts:
        flush()
    return lines


def _needs_space(prev: str, nxt: str) -> bool:
    return prev[-1:].isascii() and prev[-1:].isalnum() and nxt[:1].isascii() and nxt[:1].isalnum()


def chunk_lines(lines: Sequence[str], limit: int = CHUNK_CHARS) -> list[str]:
    """Group lines into chunks of at most ``limit`` characters; an over-long line is cut."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        pieces = [line[i : i + limit] for i in range(0, len(line), limit)] or [line]
        for piece in pieces:
            if buf and size + len(piece) + 1 > limit:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(piece)
            size += len(piece) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


_REF = re.compile(r"\[?\s*#?\s*(\d{1,7})(?:\s+[\d:]+)?\s*\]?")


def _refs(value: Any, known: Mapping[str, str] | None) -> list[str]:
    """``["#12", 15, "#3 00:41"]`` -> the mapped values of the known references, deduplicated,
    in line order, at most ``MAX_REFS``. Anything else is dropped."""
    if known is None or value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    if len(raw) == 1 and isinstance(raw[0], str) and "," in raw[0]:
        raw = raw[0].split(",")  # "#12, #15"
    found: list[int] = []
    for item in raw[:20]:
        if isinstance(item, bool) or not isinstance(item, int | str):
            continue
        m = _REF.fullmatch(str(item).strip())
        if m and m.group(1).lstrip("0") in known and int(m.group(1)) not in found:
            found.append(int(m.group(1)))
    return [known[str(n)] for n in sorted(found)[:MAX_REFS]]


def _points(value: Any, known: Mapping[str, str] | None) -> list[SummaryPoint]:
    if not isinstance(value, list):
        return []
    out: list[SummaryPoint] = []
    for item in value:
        if isinstance(item, dict):
            text = _text(item.get("text", item.get("content")), MAX_ITEM_CHARS)
            refs = _refs(item.get("refs"), known)
        elif isinstance(item, list):
            continue
        else:
            text, refs = _text(item, MAX_ITEM_CHARS), []
        if text:
            out.append(SummaryPoint(text=text, refs=refs))
    return out[:MAX_ITEMS]


def _optional(value: Any) -> str | None:
    text = _text(value, 100)
    return None if text.casefold() in ("", "null", "none", "无", "未指定", "未明确") else text


def parse_summary(
    answer: dict[str, Any],
    *,
    model: str,
    chunks: int = 1,
    refs: Mapping[str, str] | None = None,
) -> TranscriptSummary:
    """Validate and clamp the model's JSON (wrong types are dropped, not trusted).

    ``refs`` maps the line references the model may cite (``"12"``, without ``#``) to what is
    stored for them; references not in it are dropped, and ``None`` drops all of them.
    """
    actions: list[ActionItem] = []
    raw_actions = answer.get("action_items")
    for item in raw_actions if isinstance(raw_actions, list) else []:
        if isinstance(item, dict):
            task = _text(item.get("task"), MAX_ITEM_CHARS)
            owner, due = _optional(item.get("owner")), _optional(item.get("due"))
            cited = _refs(item.get("refs"), refs)
        elif isinstance(item, list):
            continue
        else:
            task, owner, due, cited = _text(item, MAX_ITEM_CHARS), None, None, []
        if task:
            actions.append(ActionItem(task=task, owner=owner, due=due, refs=cited))
    summary = TranscriptSummary(
        overview=_text(answer.get("overview"), MAX_OVERVIEW_CHARS),
        key_points=_points(answer.get("key_points"), refs),
        decisions=_points(answer.get("decisions"), refs),
        action_items=actions[:MAX_ITEMS],
        model=model,
        chunks=chunks,
        created_at=datetime.now(UTC),
    )
    if not (summary.overview or summary.key_points):
        raise ProviderError("LLM returned an empty summary")
    return summary


def _cited(summary: TranscriptSummary) -> set[str]:
    items = [*summary.key_points, *summary.decisions, *summary.action_items]
    return {ref for item in items for ref in item.refs}


class Summarizer:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str = DEFAULT_LLM_MODEL,
        provider: LlmProviderName = "dashscope",
        http: httpx.Client | None = None,
        chunk_chars: int = CHUNK_CHARS,
    ) -> None:
        self.url = chat_url(api_key, endpoint, provider)
        self._key = api_key
        self.model = model
        self.provider: LlmProviderName = provider
        self._json_mode = True  # off once the server has rejected response_format
        self.chunk_chars = chunk_chars
        self._http = http  # None: a short-lived client per request, closed right after

    def _ask(self, system: str, user: str) -> dict[str, Any]:
        with llm_client(self._http, timeout=180) as http:
            answer, self._json_mode = chat_json_answer(
                http,
                self.url,
                self._key,
                model=self.model,
                system=system,
                user=user,
                provider=self.provider,
                json_mode=self._json_mode,
            )
            return answer

    def summarize(
        self,
        segments: Sequence[Segment],
        speakers: Sequence[Speaker],
        terms: Sequence[Term] = (),
        context: str | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> TranscriptSummary:
        source = transcript_lines(segments, speakers)
        if not source:
            raise ProviderError("The transcript has no text to summarise")
        lines = [line.text for line in source]
        if sum(len(line) + 1 for line in lines) > MAX_TRANSCRIPT_CHARS:
            raise ProviderError(
                f"The transcript is too long to summarise (> {MAX_TRANSCRIPT_CHARS} characters)"
            )
        header = []
        names = sorted({s.name for s in speakers if any(g.speaker_id == s.id for g in segments)})
        if names:
            header.append("说话人：" + "、".join(names))
        if terms:
            header.append("术语表：\n" + glossary(terms))
        if context:
            header.append("背景：" + context)
        chunks = chunk_lines(lines, self.chunk_chars)
        steps = len(chunks) + (1 if len(chunks) > 1 else 0)
        segment_of = {line.ref: line.segment_id for line in source}
        partials: list[dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            part = f"（第 {i + 1}/{len(chunks)} 部分）" if len(chunks) > 1 else ""
            user = "\n\n".join([*header, f"转写{part}：\n{chunk}"])
            partials.append(self._ask(SYSTEM_PROMPT, user))
            if progress:
                progress(i + 1, steps)
        if len(partials) == 1:
            return parse_summary(partials[0], model=self.model, refs=segment_of)
        # Clean the partials first so the merge prompt only carries the expected fields; a
        # chunk of small talk may legitimately summarise to nothing. A chunk may only cite its
        # own lines; the references stay in their "#n" form for the merge call.
        cleaned = []
        cited: set[str] = set()
        for p, chunk in zip(partials, chunks, strict=True):
            found = re.findall(r"^\[#(\d+) ", chunk, flags=re.MULTILINE)
            own = {m: f"#{m}" for m in found if m in segment_of}
            try:
                part_summary = parse_summary(p, model=self.model, refs=own)
            except ProviderError:
                continue
            cited |= _cited(part_summary)
            cleaned.append(
                part_summary.model_dump(
                    mode="json", include={"overview", "key_points", "decisions", "action_items"}
                )
            )
        if not cleaned:
            raise ProviderError("LLM returned an empty summary")
        user = "\n\n".join(
            [*header[:1], "部分摘要：\n" + json.dumps(cleaned, ensure_ascii=False, indent=1)]
        )
        merged = self._ask(MERGE_PROMPT, user)
        if progress:
            progress(steps, steps)
        allowed = {ref[1:]: segment_of[ref[1:]] for ref in cited}
        return parse_summary(merged, model=self.model, chunks=len(chunks), refs=allowed)


def cited_start(refs: Sequence[str], starts: Mapping[str, float]) -> float | None:
    """Start time of the first cited segment that still exists (``starts``: id -> seconds)."""
    return next((starts[r] for r in refs if r in starts), None)


def _hms(seconds: float) -> str:
    total = max(int(seconds), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def summary_markdown(
    summary: TranscriptSummary, *, title: str = "摘要", segments: Sequence[Segment] = ()
) -> str:
    """Plain Markdown rendering (CLI output). With ``segments``, a cited item ends with the start
    time of its first cited segment, ``（00:03:41）``."""
    starts = {s.id: s.start for s in segments}

    def when(refs: Sequence[str]) -> str:
        start = cited_start(refs, starts)
        return "" if start is None else f"（{_hms(start)}）"

    out = [f"# {title}", "", summary.overview, ""]
    sections = (("要点", summary.key_points), ("决定", summary.decisions))
    for name, items in sections:
        if items:
            out += [f"## {name}", "", *[f"- {i.text}{when(i.refs)}" for i in items], ""]
    if summary.action_items:
        out += ["## 待办", ""]
        for a in summary.action_items:
            extra = "，".join(x for x in (a.owner, a.due) if x)
            out.append(f"- {a.task}" + (f"（{extra}）" if extra else "") + when(a.refs))
        out.append("")
    return "\n".join(out)
