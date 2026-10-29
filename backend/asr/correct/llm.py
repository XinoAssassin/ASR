"""[5b] L3: LLM correction *suggestions* (docs/research/post-correction.md §1.4, §3.4).

Opt-in and never auto-applied: the model sees batches of <= 20 consecutive segments with the
term glossary (pinyin for Chinese terms) and context, must return the same segment ids, and may only
fix mis-heard words. A segment whose suggestion changes more than ``MAX_EDIT_RATIO`` of its text is
discarded; edits that write a glossary term do not count towards that limit. Each changed span
becomes a ``Correction(source="llm", status="suggested")``.

Transport: DashScope OpenAI-compatible chat (``{endpoint}/compatible-mode/v1/chat/completions``)
with the same key and allow-listed endpoint as the cloud ASR provider.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable, Sequence

import httpx

from asr.providers.base import Correction, ProviderError, Segment, Term
from asr.providers.cloud import is_allowed_endpoint

BATCH_SIZE = 20
MAX_EDIT_RATIO = 0.15
DEFAULT_LLM_MODEL = "qwen-plus"

SYSTEM_PROMPT = """你是会议转写的校对助手。输入是语音识别结果，按 id 给出若干句子。
只修正明显的听错（同音字、近音词、英文术语被写成中文谐音、专有名词写法），参考术语表和拼音。
严格规则：
- 不增删内容，不改写措辞，不润色，不改标点风格，不合并或拆分句子；
- 口语、语气词、重复保留原样；
- 不确定就不改；
- 只输出 JSON：{"segments": [{"id": "...", "text": "..."}]}，只包含有修改的句子，id 与输入一致。"""


def glossary(terms: Sequence[Term]) -> str:
    """One line per term: ``term (pinyin) — note``; English terms have no pinyin."""
    from pypinyin import lazy_pinyin

    lines = []
    for t in terms:
        has_cjk = any("一" <= ch <= "鿿" for ch in t.term)
        line = t.term + (f" ({' '.join(lazy_pinyin(t.term))})" if has_cjk else "")
        if t.aliases:
            line += f"；常被听成：{'、'.join(t.aliases)}"
        if t.note:
            line += f" — {t.note}"
        lines.append(line)
    return "\n".join(lines)


def edit_ratio(old: str, new: str) -> float:
    return 1.0 - difflib.SequenceMatcher(None, old, new, autojunk=False).ratio()


def unsanctioned_ratio(old: str, corrections: Sequence[Correction], terms: Sequence[Term]) -> float:
    """Share of ``old`` changed by edits that do *not* write a glossary term.

    Replacing a mis-heard span with a term ("汤泡" -> "Townfall") is the point of the pass and is
    exempt; anything else counts towards ``MAX_EDIT_RATIO``.
    """
    names = [t.term.casefold() for t in terms if t.term]
    changed = sum(
        max(len(c.before), 1)
        for c in corrections
        if not any(name in c.after.casefold() for name in names)
    )
    return changed / max(len(old), 1)


def _squash(text: str) -> str:
    return "".join(text.split())


def _trim_padding(before: str, after: str) -> str:
    """Drop whitespace the model put around a replacement when the original had none there."""
    if not before[:1].isspace():
        after = after.lstrip()
    if not before[-1:].isspace():
        after = after.rstrip()
    return after


def diff_corrections(segment_id: str, old: str, new: str) -> list[Correction]:
    """Changed spans of ``old`` -> ``new``; edits separated by <= 1 equal char are joined.

    Whitespace-only edits are dropped (models like to space out Latin words in Chinese text).
    """
    spans: list[list[int]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        if spans and i1 - spans[-1][1] <= 1:
            spans[-1][1], spans[-1][3] = i2, j2
        else:
            spans.append([i1, i2, j1, j2])
    return [
        Correction(
            segment_id=segment_id,
            source="llm",
            before=old[i1:i2],
            after=_trim_padding(old[i1:i2], new[j1:j2]),
            offset=i1,
            status="suggested",
        )
        for i1, i2, j1, j2 in spans
        if _squash(old[i1:i2]) != _squash(new[j1:j2])
    ]


class LlmCorrector:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str = DEFAULT_LLM_MODEL,
        http: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError("DashScope API key is not configured")
        if not is_allowed_endpoint(endpoint):
            raise ProviderError("DashScope endpoint is not in the allow-list")
        self._key = api_key
        self.url = endpoint.rstrip("/") + "/compatible-mode/v1/chat/completions"
        self.model = model
        self._http = http or httpx.Client(timeout=120, follow_redirects=False)

    def _ask(
        self, payload: list[dict[str, str]], terms: Sequence[Term], context: str | None
    ) -> dict[str, str]:
        user = []
        if terms:
            user.append("术语表：\n" + glossary(terms))
        if context:
            user.append("背景：" + context)
        user.append("句子：\n" + json.dumps({"segments": payload}, ensure_ascii=False))
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(user)},
            ],
        }
        try:
            r = self._http.post(
                self.url, json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"LLM request failed: {type(exc).__name__}") from None
        if r.status_code in (401, 403):
            raise ProviderError("DashScope rejected the API key for the LLM")
        if r.status_code != 200:
            raise ProviderError(f"LLM request failed (HTTP {r.status_code})")
        try:
            content = r.json()["choices"][0]["message"]["content"]
            items = json.loads(content).get("segments", [])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("LLM returned an unreadable answer") from None
        return {
            str(i["id"]): str(i["text"])
            for i in items
            if isinstance(i, dict) and "id" in i and "text" in i
        }

    def suggest(
        self,
        segments: Sequence[Segment],
        terms: Sequence[Term] = (),
        context: str | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[Correction], int]:
        """Suggestions for ``segments`` and how many segment rewrites were discarded."""
        ordered = sorted(segments, key=lambda s: s.start)
        corrections: list[Correction] = []
        discarded = 0
        for b in range(0, len(ordered), BATCH_SIZE):
            batch = ordered[b : b + BATCH_SIZE]
            answer = self._ask([{"id": s.id, "text": s.text} for s in batch], terms, context)
            by_id = {s.id: s for s in batch}
            for sid, text in answer.items():
                seg = by_id.get(sid)
                if seg is None or not text.strip() or text == seg.text:
                    continue
                found = diff_corrections(sid, seg.text, text)
                if unsanctioned_ratio(seg.text, found, terms) > MAX_EDIT_RATIO:
                    discarded += 1
                    continue
                corrections += found
            if progress:
                progress(min(b + BATCH_SIZE, len(ordered)), len(ordered))
        return corrections, discarded
