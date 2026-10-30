"""[5] Merge recognised text, token timestamps and speaker turns into transcript segments.

Each token (a CJK character or a Latin word, with absolute start / end seconds) takes the speaker
whose turns overlap it most; single-token flips between two identical neighbours are smoothed away.
Segments break at sentence-final punctuation and at speaker changes, and their text is cut from the
ASR output so punctuation and spacing survive. Every character of a chunk's text lands in exactly
one segment: text the tokens do not cover (``《`` before the first token, ``++`` / ``℃`` / emoji
the aligner drops, anything after the last token) is attached to a neighbour, so the segment texts
of a chunk concatenate back to the chunk text (modulo whitespace at the cuts). Pure functions, no
models.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from asr.providers.base import Segment, Word

SENTENCE_END = frozenset("。！？!?；;…")
CLAUSE_BREAK = SENTENCE_END | frozenset("，,、：:")
SNAP_TOKENS = 4  # a speaker change may move this many tokens to a better boundary
MIN_RUN_TOKENS = 2  # shorter speaker runs are absorbed by a neighbour ...
MIN_RUN_SECONDS = 0.4  # ... unless they last at least this long
PAUSE_SPLIT_S = 0.8  # a silence this long starts a new segment (punctuation-free engines)
MIN_SEGMENT_S = 0.08  # the aligner's frame; a zero-length token still gets this much time


@dataclass(frozen=True)
class Token:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


@dataclass
class ChunkResult:
    """One ASR chunk: its text (with punctuation) and aligned tokens in absolute seconds."""

    start: float
    end: float
    text: str
    tokens: list[Token] = field(default_factory=list)
    speaker: str | None = None  # fixed speaker (single-speaker channel); skips turn lookup


def _fold(ch: str) -> str:
    return unicodedata.normalize("NFKC", ch).lower()


def _aligner_kept(ch: str) -> bool:
    """Characters Qwen3-ForcedAligner keeps in its tokens: letters, numbers, CJK, apostrophes.

    Everything else (``-`` ``.`` ``+`` ``%`` ``$`` ...) is dropped *inside* a word, so ``GPT-4``
    becomes the token ``GPT4``; mirrors ``_is_kept_char`` in transformers' Qwen3-ASR processor.
    """
    return ch == "'" or unicodedata.category(ch)[0] in "LN" or _is_cjk(ch)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2CEAF
        or 0xF900 <= cp <= 0xFAFF
        or 0x2F800 <= cp <= 0x2FA1F
    )


def _match_at(folded: str, text: str, i: int, needle: str) -> int:
    """End index of ``needle`` matched from ``folded[i]``, skipping characters the aligner drops
    (never whitespace, never before the first character); ``-1`` when it does not match."""
    j = i
    for k, c in enumerate(needle):
        while (
            k > 0
            and j < len(folded)
            and folded[j] != c
            and not text[j].isspace()
            and not _aligner_kept(text[j])
        ):
            j += 1
        if j >= len(folded) or folded[j] != c:
            return -1
        j += 1
    return j


def token_spans(text: str, tokens: Sequence[Token]) -> list[tuple[int, int]] | None:
    """Character span of every token in ``text`` (matched in order, case / width-insensitive).

    A token may omit punctuation the text has inside the word (aligner ``GPT4`` for ``GPT-4``,
    ``35`` for ``3.5``, ``realtime`` for ``real-time``); its span then covers those characters.
    Returns ``None`` if a token cannot be found, so callers fall back to chunk-level segments.
    """
    folded = "".join(_fold(c) if len(_fold(c)) == 1 else c for c in text)
    spans: list[tuple[int, int]] = []
    pos = 0
    for tok in tokens:
        # Same fold rule as the text: a character whose fold is several characters (``½``, ``ﬁ``,
        # ``İ``) stays as it is on both sides, so the two still match.
        needle = "".join(_fold(c) if len(_fold(c)) == 1 else c for c in tok.text)
        if not needle:
            spans.append((pos, pos))
            continue
        idx = folded.find(needle[0], pos)
        end = -1
        while idx >= 0:
            end = _match_at(folded, text, idx, needle)
            if end >= 0:
                break
            idx = folded.find(needle[0], idx + 1)
        if idx < 0:
            return None
        spans.append((idx, end))
        pos = end
    return spans


def speaker_for(start: float, end: float, turns: Sequence[SpeakerTurn]) -> str | None:
    """Speaker with the largest overlap; the nearest turn when nothing overlaps."""
    if not turns:
        return None
    best, best_overlap = None, 0.0
    for t in turns:
        overlap = min(end, t.end) - max(start, t.start)
        if overlap > best_overlap:
            best, best_overlap = t.speaker, overlap
    if best is not None:
        return best
    mid = (start + end) / 2
    return min(turns, key=lambda t: min(abs(mid - t.start), abs(mid - t.end))).speaker


def smooth_flips(labels: list[str]) -> list[str]:
    """``A B A`` -> ``A A A``: a lone token between two identical neighbours joins them."""
    out = list(labels)
    for i in range(1, len(out) - 1):
        if out[i - 1] == out[i + 1] != out[i]:
            out[i] = out[i - 1]
    return out


def _runs(labels: list[str]) -> list[tuple[int, int]]:
    """``[start, end)`` index ranges of equal consecutive labels."""
    runs, start = [], 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[start]:
            runs.append((start, i))
            start = i
    return runs


def absorb_short_runs(labels: list[str], tokens: Sequence[Token]) -> list[str]:
    """Merge speaker runs shorter than ``MIN_RUN_TOKENS`` tokens and ``MIN_RUN_SECONDS`` into the
    longer neighbouring run (diarization windows are 1.5 s, so tiny runs are mostly noise)."""
    out = list(labels)
    changed = True
    while changed:
        changed = False
        runs = _runs(out)
        if len(runs) < 2:
            break
        for k, (a, b) in enumerate(runs):
            dur = tokens[b - 1].end - tokens[a].start
            if b - a >= MIN_RUN_TOKENS or dur >= MIN_RUN_SECONDS:
                continue
            neighbours = [runs[j] for j in (k - 1, k + 1) if 0 <= j < len(runs)]
            target = max(neighbours, key=lambda r: tokens[r[1] - 1].end - tokens[r[0]].start)
            out[a:b] = [out[target[0]]] * (b - a)
            changed = True
            break
    return out


def snap_changes(
    labels: list[str], tokens: Sequence[Token], text: str, spans: Sequence[tuple[int, int]]
) -> list[str]:
    """Move each speaker change to the best boundary within ``SNAP_TOKENS`` tokens: a punctuation
    mark if there is one (nearest wins), otherwise the longest pause between tokens.

    A change never moves onto or past a neighbouring change, so every run keeps at least one
    token: snapping does not erase a short run that lies between two changes.
    """
    out = list(labels)
    changes = [a for a, _ in _runs(out)[1:]]
    prev = 0  # where the previous change ended up after snapping
    for k, a in enumerate(changes):
        nxt = changes[k + 1] if k + 1 < len(changes) else len(out)
        best = _snap_target(
            a, max(a - SNAP_TOKENS, 1, prev + 1), min(a + SNAP_TOKENS, nxt - 1), tokens, text, spans
        )
        if best != a:
            before, after = out[a - 1], out[a]
            if best < a:
                out[best:a] = [after] * (a - best)
            else:
                out[a:best] = [before] * (best - a)
        prev = best
    return out


def _snap_target(
    a: int,
    lo: int,
    hi: int,
    tokens: Sequence[Token],
    text: str,
    spans: Sequence[tuple[int, int]],
) -> int:
    """Best position in ``[lo, hi]`` for the change at ``a`` (``a`` itself if none is better)."""
    if hi < lo:
        return a
    candidates = range(lo, hi + 1)

    def punct(i: int) -> bool:
        return any(c in CLAUSE_BREAK for c in text[spans[i - 1][1] : spans[i][0]])

    punctuated = [i for i in candidates if punct(i)]
    if punctuated:
        return min(punctuated, key=lambda i: abs(i - a))
    best = max(candidates, key=lambda i: (tokens[i].start - tokens[i - 1].end, -abs(i - a)))
    if tokens[best].start - tokens[best - 1].end <= 0.05:
        return a  # no real pause nearby: keep the diarization boundary
    return best


def _opens(ch: str) -> bool:
    """Characters that belong to the *following* token: opening brackets / quotes (``《“(``) and
    currency signs (``$100``, ``¥5``)."""
    return unicodedata.category(ch) in ("Ps", "Pi", "Sc")


def _trailing_end(text: str, end: int) -> int:
    """Extend a span over what follows it up to the next token, space or opening mark: closing
    punctuation and the characters the aligner drops from its tokens (``C++``, ``30℃``, emoji)."""
    while end < len(text):
        ch = text[end]
        if ch.isspace() or _aligner_kept(ch) or _opens(ch):
            break
        end += 1
    return end


def _sentence_end(text: str, start: int, end: int) -> bool:
    """CJK / ``!?;`` sentence ends anywhere; a Latin full stop only before a space or the end
    of the text, so ``3.5`` and ``v1.2`` do not split (English output, M11 Whisper route)."""
    trail = text[start:end]
    if any(c in SENTENCE_END for c in trail):
        return True
    return "." in trail and (end == len(text) or text[end].isspace())


def build_segments(
    chunks: Sequence[ChunkResult], turns: Sequence[SpeakerTurn], *, default_speaker: str
) -> list[Segment]:
    segments: list[Segment] = []
    cap = 0.0  # end of the chunk being emitted; a segment never extends past it

    def emit(speaker: str, start: float, end: float, text: str, words: list[Word]) -> None:
        text = text.strip()
        end = min(max(end, start + MIN_SEGMENT_S), max(cap, start))
        if text:
            segments.append(
                Segment(
                    id=f"seg_{len(segments):05d}",
                    speaker_id=speaker,
                    start=round(start, 3),
                    end=round(end, 3),
                    text=text,
                    words=words,
                )
            )

    for chunk in chunks:
        cap = chunk.end
        spans = token_spans(chunk.text, chunk.tokens) if chunk.tokens else None
        if not spans:
            speaker = chunk.speaker or speaker_for(chunk.start, chunk.end, turns) or default_speaker
            emit(speaker, chunk.start, chunk.end, chunk.text, [])
            continue
        labels = [
            chunk.speaker or speaker_for(t.start, t.end, turns) or default_speaker
            for t in chunk.tokens
        ]
        labels = smooth_flips(labels)
        labels = absorb_short_runs(labels, chunk.tokens)
        labels = snap_changes(labels, chunk.tokens, chunk.text, spans)
        # Decide the groups first, then cut the text at their ends: each group starts where the
        # previous one stopped (0 for the first) and the last one runs to the end of the text.
        groups: list[tuple[list[int], int]] = []
        group: list[int] = []
        for i in range(len(chunk.tokens)):
            paused = i > 0 and chunk.tokens[i].start - chunk.tokens[i - 1].end >= PAUSE_SPLIT_S
            if group and (labels[i] != labels[group[0]] or paused):
                groups.append((group, _trailing_end(chunk.text, spans[group[-1]][1])))
                group = []
            group.append(i)
            end_char = _trailing_end(chunk.text, spans[i][1])
            if _sentence_end(chunk.text, spans[i][1], end_char):
                groups.append((group, end_char))
                group = []
        if group:
            groups.append((group, len(chunk.text)))
        if groups:
            groups[-1] = (groups[-1][0], len(chunk.text))
        cursor = 0
        for grp, end_char in groups:
            _emit_group(chunk, labels, grp, cursor, end_char, emit)
            cursor = end_char
    return segments


def _emit_group(
    chunk: ChunkResult,
    labels: list[str],
    group: list[int],
    start_char: int,
    end_char: int,
    emit: Callable[[str, float, float, str, list[Word]], None],
) -> None:
    first, final = group[0], group[-1]
    words = [
        Word(
            text=chunk.tokens[i].text,
            start=round(chunk.tokens[i].start, 3),
            end=round(chunk.tokens[i].end, 3),
        )
        for i in group
    ]
    emit(
        labels[first],
        chunk.tokens[first].start,
        chunk.tokens[final].end,
        chunk.text[start_char:end_char],
        words,
    )
