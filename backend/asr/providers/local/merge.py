"""[5] Merge recognised text, token timestamps and speaker turns into transcript segments.

Each token (a CJK character or a Latin word, with absolute start / end seconds) takes the speaker
whose turns overlap it most; single-token flips between two identical neighbours are smoothed away.
Segments break at sentence-final punctuation and at speaker changes, and their text is cut from the
ASR output so punctuation and spacing survive. Pure functions, no models.
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


def token_spans(text: str, tokens: Sequence[Token]) -> list[tuple[int, int]] | None:
    """Character span of every token in ``text`` (matched in order, case / width-insensitive).

    Returns ``None`` if a token cannot be found, so callers fall back to chunk-level segments.
    """
    folded = "".join(_fold(c) if len(_fold(c)) == 1 else c for c in text)
    spans: list[tuple[int, int]] = []
    pos = 0
    for tok in tokens:
        needle = "".join(_fold(c) for c in tok.text)
        idx = folded.find(needle, pos)
        if idx < 0:
            return None
        spans.append((idx, idx + len(needle)))
        pos = idx + len(needle)
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
    mark if there is one (nearest wins), otherwise the longest pause between tokens."""
    out = list(labels)
    for a, _ in _runs(out)[1:]:
        lo, hi = max(a - SNAP_TOKENS, 1), min(a + SNAP_TOKENS, len(out) - 1)
        if hi < lo:
            continue
        candidates = range(lo, hi + 1)

        def punct(i: int) -> bool:
            return any(c in CLAUSE_BREAK for c in text[spans[i - 1][1] : spans[i][0]])

        punctuated = [i for i in candidates if punct(i)]
        if punctuated:
            best = min(punctuated, key=lambda i: abs(i - a))
        else:
            best = max(candidates, key=lambda i: (tokens[i].start - tokens[i - 1].end, -abs(i - a)))
            if tokens[best].start - tokens[best - 1].end <= 0.05:
                continue  # no real pause nearby: keep the diarization boundary
        if best == a:
            continue
        before, after = out[a - 1], out[a]
        if best < a:
            out[best:a] = [after] * (a - best)
        else:
            out[a:best] = [before] * (best - a)
    return out


def _trailing_end(text: str, end: int) -> int:
    """Extend a span over following punctuation / closing quotes (not over spaces or letters)."""
    while end < len(text):
        cat = unicodedata.category(text[end])
        if cat[0] == "P" or text[end] in "”’》）)】」』":
            end += 1
        else:
            break
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
        group: list[int] = []
        for i in range(len(chunk.tokens)):
            paused = i > 0 and chunk.tokens[i].start - chunk.tokens[i - 1].end >= PAUSE_SPLIT_S
            if group and (labels[i] != labels[group[0]] or paused):
                _emit_group(chunk, spans, labels, group, emit)
                group = []
            group.append(i)
            end_char = _trailing_end(chunk.text, spans[i][1])
            if _sentence_end(chunk.text, spans[i][1], end_char):
                _emit_group(chunk, spans, labels, group, emit)
                group = []
        if group:
            _emit_group(chunk, spans, labels, group, emit, last=True)
    return segments


def _emit_group(
    chunk: ChunkResult,
    spans: list[tuple[int, int]],
    labels: list[str],
    group: list[int],
    emit: Callable[[str, float, float, str, list[Word]], None],
    last: bool = False,
) -> None:
    first, final = group[0], group[-1]
    start_char = spans[first][0]
    end_char = len(chunk.text) if last else _trailing_end(chunk.text, spans[final][1])
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
