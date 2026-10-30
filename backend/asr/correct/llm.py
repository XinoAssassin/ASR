"""[5b] L3: LLM correction *suggestions* (docs/research/post-correction.md §1.4, §3.4).

Opt-in and never auto-applied: the model sees batches of <= 20 consecutive segments with the
term glossary (pinyin for Chinese terms) and context, must return the same segment ids, and may only
fix mis-heard words. The rewrite is diffed on tokens (whole Latin words, single CJK characters,
whitespace runs); each change weighs ``max(len(before), len(after))`` code points, and a segment
whose changes exceed ``MAX_EDIT_RATIO`` of its text is discarded. Only a change that writes a
glossary term (its ``after`` is the term, give or take ``TERM_SLACK`` characters) is exempt. Each
changed span becomes a ``Correction(source="llm", status="suggested")``.

Transport: DashScope OpenAI-compatible chat (``{endpoint}/compatible-mode/v1/chat/completions``)
with the same key and allow-listed endpoint as the cloud ASR provider.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from asr.providers.base import Correction, ProviderError, Segment, Term
from asr.providers.cloud import is_allowed_endpoint

BATCH_SIZE = 20
MAX_EDIT_RATIO = 0.15
TERM_SLACK = 2  # characters an exempt term change may carry besides the term itself
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


def _writes_term(after: str, names: Sequence[str]) -> bool:
    """``after`` (whitespace-stripped) is a glossary term, give or take ``TERM_SLACK`` chars."""
    text = after.strip().casefold()
    return any(name in text and len(text) - len(name) <= TERM_SLACK for name in names)


def unsanctioned_ratio(old: str, corrections: Sequence[Correction], terms: Sequence[Term]) -> float:
    """Share of ``old`` changed by edits that do *not* write a glossary term.

    Each change weighs ``max(len(before), len(after))``, so insertions count in full. Replacing a
    mis-heard span with a term ("汤泡" -> "Townfall") is the point of the pass and is exempt;
    anything else counts towards ``MAX_EDIT_RATIO``.
    """
    names = [t.term.strip().casefold() for t in terms if t.term.strip()]
    changed = sum(
        max(len(c.before), len(c.after), 1) for c in corrections if not _writes_term(c.after, names)
    )
    return changed / max(len(old), 1)


# Latin words (letters / digits, with "-" or "." inside), whitespace runs, any other single char.
_TOKEN = re.compile(r"[0-9A-Za-zÀ-ɏ](?:[0-9A-Za-zÀ-ɏ.\-]*[0-9A-Za-zÀ-ɏ])?|\s+|.", re.DOTALL)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _squash(text: str) -> str:
    return "".join(text.split())


def _latin(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _trim(old: str, i1: int, i2: int, after: str) -> str:
    """Drop whitespace the model put at the edges of a replacement for ``old[i1:i2]``.

    Edge whitespace is kept when ``before`` had it there too, or when it is needed to separate
    two Latin words and the neighbouring original text has no space there.
    """
    before = old[i1:i2]
    core = after.strip()
    if not core:
        return after
    if after[:1].isspace() and not before[:1].isspace():
        left = old[i1 - 1 : i1]
        if left.isspace() or not (_latin(left) and _latin(core[0])):
            after = after.lstrip()
    if after[-1:].isspace() and not before[-1:].isspace():
        right = old[i2 : i2 + 1]
        if right.isspace() or not (_latin(right) and _latin(core[-1])):
            after = after.rstrip()
    return after


def diff_corrections(segment_id: str, old: str, new: str) -> list[Correction]:
    """Changed spans of ``old`` -> ``new``, diffed on tokens (whole Latin words, single CJK
    characters, whitespace runs); edits separated by <= 1 equal token are joined.

    Whitespace shared by both ends of a span is left out of it; whitespace-only edits are dropped
    (models like to space out Latin words in Chinese text).
    """
    a, b = _tokens(old), _tokens(new)
    a_pos = [0]
    for t in a:
        a_pos.append(a_pos[-1] + len(t))
    b_pos = [0]
    for t in b:
        b_pos.append(b_pos[-1] + len(t))
    spans: list[list[int]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if spans and i1 - spans[-1][1] <= 1:
            spans[-1][1], spans[-1][3] = i2, j2
        else:
            spans.append([i1, i2, j1, j2])
    out = []
    for i1, i2, j1, j2 in spans:
        # Whitespace tokens both sides start / end with are context, not part of the change.
        while i1 < i2 and j1 < j2 and a[i1] == b[j1] and a[i1].isspace():
            i1, j1 = i1 + 1, j1 + 1
        while i1 < i2 and j1 < j2 and a[i2 - 1] == b[j2 - 1] and a[i2 - 1].isspace():
            i2, j2 = i2 - 1, j2 - 1
        c1, c2 = a_pos[i1], a_pos[i2]
        before, after = old[c1:c2], new[b_pos[j1] : b_pos[j2]]
        if _squash(before) == _squash(after):
            continue
        out.append(
            Correction(
                segment_id=segment_id,
                source="llm",
                before=before,
                after=_trim(old, c1, c2, after),
                offset=c1,
                status="suggested",
            )
        )
    return out


def chat_url(api_key: str, endpoint: str) -> str:
    """Validated chat-completions URL for ``endpoint`` (same allow-list as the cloud ASR)."""
    if not api_key:
        raise ProviderError("DashScope API key is not configured")
    if not is_allowed_endpoint(endpoint):
        raise ProviderError("DashScope endpoint is not in the allow-list")
    return endpoint.rstrip("/") + "/compatible-mode/v1/chat/completions"


def chat_json(
    http: httpx.Client,
    url: str,
    api_key: str,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0,
) -> dict[str, Any]:
    """One JSON-mode chat completion; errors are ``ProviderError`` without secrets."""
    body = {
        "model": model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        r = http.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ProviderError(f"LLM request failed: {type(exc).__name__}") from None
    if r.status_code in (401, 403):
        raise ProviderError("DashScope rejected the API key for the LLM")
    if r.status_code != 200:
        raise ProviderError(f"LLM request failed (HTTP {r.status_code})")
    try:
        answer = json.loads(r.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderError("LLM returned an unreadable answer") from None
    if not isinstance(answer, dict):
        raise ProviderError("LLM returned an unreadable answer")
    return answer


class LlmCorrector:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str = DEFAULT_LLM_MODEL,
        http: httpx.Client | None = None,
    ) -> None:
        self.url = chat_url(api_key, endpoint)
        self._key = api_key
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
        answer = chat_json(
            self._http,
            self.url,
            self._key,
            model=self.model,
            system=SYSTEM_PROMPT,
            user="\n\n".join(user),
        )
        items = answer.get("segments", [])
        if not isinstance(items, list):
            raise ProviderError("LLM returned an unreadable answer")
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
