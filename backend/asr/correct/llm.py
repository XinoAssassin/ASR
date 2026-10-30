"""[5b] L3: LLM correction *suggestions* (docs/research/post-correction.md §1.4, §3.4).

Opt-in and never auto-applied: the model sees batches of <= 20 consecutive segments with the
term glossary (pinyin for Chinese terms) and context, must return the same segment ids, and may only
fix mis-heard words. The rewrite is diffed on tokens (whole Latin words, single CJK characters,
whitespace runs); each change weighs ``max(len(before), len(after))`` code points, and a segment
whose changes exceed ``MAX_EDIT_RATIO`` of its text is discarded. Only a change that writes a
glossary term (its ``after`` is the term, give or take ``TERM_SLACK`` characters, and its ``before``
is no longer than a mis-hearing of that term can be: ``max(2 * len(term), len(term) + 4)`` plus
``TERM_SLACK``) is exempt. Each changed span becomes a ``Correction(source="llm",
status="suggested")``.

Transport (``provider``):

- ``dashscope``: DashScope OpenAI-compatible chat
  (``{endpoint}/compatible-mode/v1/chat/completions``) with a DashScope key, only to an
  allow-listed endpoint (same as the cloud ASR provider, D2).
- ``openai``: any OpenAI-compatible server (llama.cpp, vLLM, Ollama, Lemonade, FastFlowLM, ...):
  ``{base_url}/chat/completions``, ``Authorization: Bearer`` only when a key is configured. The
  base URL is the operator's choice, so the text (and the key, if any) goes wherever it points.
  A server that rejects ``response_format`` gets one retry without it, and the JSON is then read
  from the text (code fences and ``<think>`` blocks stripped).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from asr.providers.base import Correction, ProviderError, Segment, Term
from asr.providers.cloud import is_allowed_endpoint

BATCH_SIZE = 20
MAX_EDIT_RATIO = 0.15
TERM_SLACK = 2  # characters an exempt term change may carry besides the term itself
DEFAULT_LLM_MODEL = "qwen-plus"

LlmProviderName = Literal["dashscope", "openai"]
LLM_PROVIDERS: tuple[LlmProviderName, ...] = ("dashscope", "openai")
MAX_BASE_URL_CHARS = 500

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


def _max_term_before(name: str) -> int:
    """Longest ``before`` a change writing ``name`` may replace and stay exempt: a mis-heard
    term (``汤泡`` -> ``Townfall``, ``tong yi`` -> ``通义``), not a rewritten clause."""
    return max(2 * len(name), len(name) + 4) + TERM_SLACK


def _writes_term(before: str, after: str, names: Sequence[str]) -> bool:
    """``after`` (whitespace-stripped) is a glossary term, give or take ``TERM_SLACK`` chars,
    and ``before`` is short enough to be a mis-hearing of it."""
    text = after.strip().casefold()
    old = len(before.strip())
    return any(
        name in text and len(text) - len(name) <= TERM_SLACK and old <= _max_term_before(name)
        for name in names
    )


def unsanctioned_ratio(old: str, corrections: Sequence[Correction], terms: Sequence[Term]) -> float:
    """Share of ``old`` changed by edits that do *not* write a glossary term.

    Each change weighs ``max(len(before), len(after))``, so insertions count in full. Replacing a
    mis-heard span with a term ("汤泡" -> "Townfall") is the point of the pass and is exempt;
    anything else counts towards ``MAX_EDIT_RATIO``.
    """
    names = [t.term.strip().casefold() for t in terms if t.term.strip()]
    changed = sum(
        max(len(c.before), len(c.after), 1)
        for c in corrections
        if not _writes_term(c.before, c.after, names)
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


def normalize_base_url(url: str) -> str:
    """Validated OpenAI-compatible base URL (``http(s)://host[:port][/path]``, e.g.
    ``http://192.168.2.10:8080/v1``) without a trailing ``/``.

    Rejects other schemes, userinfo (credentials), query strings, fragments, whitespace and
    control characters; a pasted ``.../chat/completions`` suffix is dropped. The message of the
    ``ValueError`` never contains the URL.
    """
    if not isinstance(url, str):
        raise ValueError("must be a string")
    url = url.strip()
    if not url or len(url) > MAX_BASE_URL_CHARS:
        raise ValueError(f"must be 1-{MAX_BASE_URL_CHARS} characters")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise ValueError("must not contain whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("is not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("must be an http:// or https:// URL")
    if "@" in parts.netloc:
        raise ValueError("must not contain credentials")
    if "?" in url or "#" in url:
        raise ValueError("must not contain a query string or fragment")
    if not parts.hostname or (port is not None and not 0 < port < 65536):
        raise ValueError("is not a valid URL")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    return f"{parts.scheme}://{parts.netloc}{path}"


def chat_url(api_key: str, endpoint: str, provider: LlmProviderName = "dashscope") -> str:
    """Validated chat-completions URL.

    ``dashscope``: the key is required and ``endpoint`` must be allow-listed (same as the cloud
    ASR). ``openai``: ``endpoint`` is the server's base URL (``normalize_base_url``); a key is
    optional.
    """
    if provider == "openai":
        if not endpoint:
            raise ProviderError("LLM endpoint is not configured")
        try:
            base = normalize_base_url(endpoint)
        except ValueError:
            raise ProviderError("LLM endpoint is invalid (an http(s) base URL is needed)") from None
        return base + "/chat/completions"
    if provider != "dashscope":
        raise ProviderError("LLM provider is invalid")
    if not api_key:
        raise ProviderError("DashScope API key is not configured")
    if not is_allowed_endpoint(endpoint):
        raise ProviderError("DashScope endpoint is not in the allow-list")
    return endpoint.rstrip("/") + "/compatible-mode/v1/chat/completions"


_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_json_text(content: str) -> Any:
    """JSON from a chat answer: plain, in a code fence, or after a ``<think>`` block; as a last
    resort the outermost ``{...}``. ``ValueError`` when none parses."""
    text = _THINK.sub("", content).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _rejects_json_mode(r: httpx.Response) -> bool:
    """A 400 / 422 whose body mentions ``response_format`` (server without JSON mode)."""
    if r.status_code not in (400, 422):
        return False
    try:
        return "response_format" in r.text[:4000].lower()
    except Exception:  # undecodable body
        return False


class ChatAnswer:
    """One chat completion: the message text, the model the server reports, and whether JSON
    mode (``response_format``) was used."""

    def __init__(self, content: str, model: str | None, json_mode: bool) -> None:
        self.content = content
        self.model = model
        self.json_mode = json_mode


def chat_completion(
    http: httpx.Client,
    url: str,
    api_key: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    provider: LlmProviderName = "dashscope",
    temperature: float = 0,
    max_tokens: int | None = None,
    json_mode: bool = True,
) -> ChatAnswer:
    """One chat completion; errors are ``ProviderError`` without secrets or response bodies.

    With ``json_mode`` the request carries ``response_format={"type": "json_object"}``; an
    ``openai`` server that rejects it (400 / 422 naming ``response_format``) is asked once more
    without it.
    """
    body: dict[str, Any] = {"model": model, "temperature": temperature, "messages": messages}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    label = "DashScope" if provider == "dashscope" else "The LLM server"

    def post(with_json: bool) -> httpx.Response:
        payload = {**body, "response_format": {"type": "json_object"}} if with_json else body
        try:
            return http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"LLM request failed: {type(exc).__name__}") from None

    r = post(json_mode)
    if json_mode and provider == "openai" and _rejects_json_mode(r):
        json_mode = False
        r = post(False)
    if r.status_code in (401, 403):
        raise ProviderError(f"{label} rejected the API key for the LLM")
    if r.status_code != 200:
        raise ProviderError(f"LLM request failed (HTTP {r.status_code})")
    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderError("LLM returned an unreadable answer") from None
    if not isinstance(content, str):
        raise ProviderError("LLM returned an unreadable answer")
    reported = data.get("model") if isinstance(data, dict) else None
    return ChatAnswer(content, reported if isinstance(reported, str) else None, json_mode)


def chat_json_answer(
    http: httpx.Client,
    url: str,
    api_key: str,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0,
    provider: LlmProviderName = "dashscope",
    json_mode: bool = True,
) -> tuple[dict[str, Any], bool]:
    """``chat_json`` plus whether JSON mode was used (callers keep it off for later calls once a
    server has rejected it)."""
    answer = chat_completion(
        http,
        url,
        api_key,
        model=model,
        provider=provider,
        temperature=temperature,
        json_mode=json_mode,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    try:
        parsed = parse_json_text(answer.content)
    except ValueError:
        raise ProviderError("LLM returned an unreadable answer") from None
    if not isinstance(parsed, dict):
        raise ProviderError("LLM returned an unreadable answer")
    return parsed, answer.json_mode


def chat_json(
    http: httpx.Client,
    url: str,
    api_key: str,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0,
    provider: LlmProviderName = "dashscope",
) -> dict[str, Any]:
    """One JSON-mode chat completion parsed to an object; errors are ``ProviderError`` without
    secrets."""
    parsed, _ = chat_json_answer(
        http,
        url,
        api_key,
        model=model,
        system=system,
        user=user,
        temperature=temperature,
        provider=provider,
    )
    return parsed


def check_llm(
    *,
    provider: LlmProviderName,
    api_key: str,
    endpoint: str,
    model: str,
    http: httpx.Client | None = None,
) -> tuple[bool, str, str | None]:
    """A tiny chat completion against the given LLM. Never raises; returns ``(ok, message,
    model)`` where ``model`` is the name the server reports (else the configured one). The
    message never contains the key, the request or the response body."""
    try:
        url = chat_url(api_key, endpoint, provider)
    except ProviderError as exc:
        return False, str(exc), None
    try:
        with llm_client(http, timeout=30) as client:
            answer = chat_completion(
                client,
                url,
                api_key,
                model=model,
                provider=provider,
                max_tokens=16,
                messages=[
                    {"role": "system", "content": 'Answer with the JSON object {"ok": true}.'},
                    {"role": "user", "content": "ping"},
                ],
            )
    except ProviderError as exc:
        return False, str(exc), model
    note = "" if answer.json_mode else " (no JSON mode; answers are parsed from plain text)"
    return True, f"The LLM answered{note}", answer.model or model


@contextlib.contextmanager
def llm_client(http: httpx.Client | None, *, timeout: float) -> Iterator[httpx.Client]:
    """The caller's client as is, or a fresh one that is closed on exit (never leaked)."""
    if http is not None:
        yield http
        return
    with httpx.Client(timeout=timeout, follow_redirects=False) as own:
        yield own


class LlmCorrector:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str = DEFAULT_LLM_MODEL,
        provider: LlmProviderName = "dashscope",
        http: httpx.Client | None = None,
    ) -> None:
        self.url = chat_url(api_key, endpoint, provider)
        self._key = api_key
        self.model = model
        self.provider: LlmProviderName = provider
        self._json_mode = True  # off once the server has rejected response_format
        self._http = http  # None: a short-lived client per request, closed right after

    def _ask(
        self, payload: list[dict[str, str]], terms: Sequence[Term], context: str | None
    ) -> dict[str, str]:
        user = []
        if terms:
            user.append("术语表：\n" + glossary(terms))
        if context:
            user.append("背景：" + context)
        user.append("句子：\n" + json.dumps({"segments": payload}, ensure_ascii=False))
        with llm_client(self._http, timeout=120) as http:
            answer, self._json_mode = chat_json_answer(
                http,
                self.url,
                self._key,
                model=self.model,
                system=SYSTEM_PROMPT,
                user="\n\n".join(user),
                provider=self.provider,
                json_mode=self._json_mode,
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
