"""LLM meeting summary (M11): overview, key points, decisions and action items.

Opt-in: the transcript text (speaker display names, segment text, coarse timestamps) is sent to
the configured chat model through the same transport as the [5b] L3 correction pass
(``asr.correct.llm``): DashScope (paid; key and allow-listed endpoint of the cloud ASR) or an
OpenAI-compatible server of the operator's choice. Short transcripts take one call. Longer ones
are cut into chunks of about ``CHUNK_CHARS`` characters at speaker-turn boundaries, each chunk is
summarised ("map") and the partial summaries are merged by one more call ("reduce").

Action-item owners are filled only when the transcript says who will do it; the model is told to
use the speaker's display name or the name mentioned in the text, never to guess.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

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


class ActionItem(BaseModel):
    task: str
    owner: str | None = Field(
        default=None, description="Speaker name or person named in the meeting, when stated."
    )
    due: str | None = Field(default=None, description="Deadline as said in the meeting.")


class TranscriptSummary(BaseModel):
    overview: str
    key_points: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    model: str = Field(description="Chat model that wrote the summary.")
    chunks: int = Field(default=1, ge=1, description="Transcript chunks summarised (map step).")
    created_at: datetime


SCHEMA_HINT = (
    '{"overview": "...", "key_points": ["..."], "decisions": ["..."], '
    '"action_items": [{"task": "...", "owner": "说话人名称或 null", "due": "期限或 null"}]}'
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
- 只依据原文，不编造，不补充原文没有的信息；术语按术语表写法，但不要在输出里提到术语表；
- 使用转写内容的主要语言输出（中文为主则用简体中文）。"""

MERGE_PROMPT = f"""你是会议纪要助手。输入是同一场会议按时间顺序分段写出的若干部分摘要（JSON）。
请合并为一份完整摘要，只输出 JSON：{SCHEMA_HINT}
要求：去掉重复，保留时间顺序；overview 概括整场会议（3-6 句）；key_points 最多 12 条；
decisions 和 action_items 全部保留（合并重复项），owner / due 只沿用部分摘要里已有的值，不要新增；
不编造；使用部分摘要的语言。"""


def _clock(seconds: float) -> str:
    total = max(int(seconds), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcript_lines(segments: Sequence[Segment], speakers: Sequence[Speaker]) -> list[str]:
    """``[mm:ss] Name: text`` per speaker turn (consecutive segments of a speaker are joined)."""
    names = {s.id: s.name for s in speakers}
    lines: list[str] = []
    current: str | None = None
    start = 0.0
    parts: list[str] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        text = seg.text.strip()
        if not text:
            continue
        if seg.speaker_id != current and parts:
            lines.append(f"[{_clock(start)}] {names.get(current or '', current)}: {''.join(parts)}")
            parts = []
        if not parts:
            current, start = seg.speaker_id, seg.start
        parts.append(text if not parts or not _needs_space(parts[-1], text) else " " + text)
    if parts:
        lines.append(f"[{_clock(start)}] {names.get(current or '', current)}: {''.join(parts)}")
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


def _items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [_text(v, MAX_ITEM_CHARS) for v in value if not isinstance(v, dict | list)]
    return [v for v in out if v][:MAX_ITEMS]


def _optional(value: Any) -> str | None:
    text = _text(value, 100)
    return None if text.casefold() in ("", "null", "none", "无", "未指定", "未明确") else text


def parse_summary(answer: dict[str, Any], *, model: str, chunks: int = 1) -> TranscriptSummary:
    """Validate and clamp the model's JSON (wrong types are dropped, not trusted)."""
    actions: list[ActionItem] = []
    raw_actions = answer.get("action_items")
    for item in raw_actions if isinstance(raw_actions, list) else []:
        if isinstance(item, dict):
            task = _text(item.get("task"), MAX_ITEM_CHARS)
            owner, due = _optional(item.get("owner")), _optional(item.get("due"))
        else:
            task, owner, due = _text(item, MAX_ITEM_CHARS), None, None
        if task:
            actions.append(ActionItem(task=task, owner=owner, due=due))
    summary = TranscriptSummary(
        overview=_text(answer.get("overview"), MAX_OVERVIEW_CHARS),
        key_points=_items(answer.get("key_points")),
        decisions=_items(answer.get("decisions")),
        action_items=actions[:MAX_ITEMS],
        model=model,
        chunks=chunks,
        created_at=datetime.now(UTC),
    )
    if not (summary.overview or summary.key_points):
        raise ProviderError("LLM returned an empty summary")
    return summary


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
        lines = transcript_lines(segments, speakers)
        if not lines:
            raise ProviderError("The transcript has no text to summarise")
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
        partials: list[dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            part = f"（第 {i + 1}/{len(chunks)} 部分）" if len(chunks) > 1 else ""
            user = "\n\n".join([*header, f"转写{part}：\n{chunk}"])
            partials.append(self._ask(SYSTEM_PROMPT, user))
            if progress:
                progress(i + 1, steps)
        if len(partials) == 1:
            return parse_summary(partials[0], model=self.model)
        # Clean the partials first so the merge prompt only carries the expected fields; a
        # chunk of small talk may legitimately summarise to nothing.
        cleaned = []
        for p in partials:
            try:
                part_summary = parse_summary(p, model=self.model)
            except ProviderError:
                continue
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
        return parse_summary(merged, model=self.model, chunks=len(chunks))


def summary_markdown(summary: TranscriptSummary, *, title: str = "摘要") -> str:
    """Plain Markdown rendering (CLI output, export)."""
    out = [f"# {title}", "", summary.overview, ""]
    sections = (("要点", summary.key_points), ("决定", summary.decisions))
    for name, items in sections:
        if items:
            out += [f"## {name}", "", *[f"- {i}" for i in items], ""]
    if summary.action_items:
        out += ["## 待办", ""]
        for a in summary.action_items:
            extra = "，".join(x for x in (a.owner, a.due) if x)
            out.append(f"- {a.task}" + (f"（{extra}）" if extra else ""))
        out.append("")
    return "\n".join(out)
