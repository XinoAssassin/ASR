"""Mixed Chinese/English error rate and term recall.

Tokens are CJK characters plus Latin words / digit runs, so "我玩Townfall" is 3 tokens; this is the
usual "mixed error rate" for code-switched Mandarin and is reported as CER in the bake-off. Text is
NFKC-normalised (full-width -> half-width), lower-cased, and punctuation / symbols / whitespace are
dropped. Numbers are not inverse-normalised: "三点" vs "3点" counts as an error, which affects all
models alike only if they share the same ITN behaviour (noted in the results).

Edited references (video subtitles) usually omit interjection fillers that a faithful model
transcribes; ``drop_fillers=True`` removes them from both sides ("lenient" CER).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_LATIN = re.compile(r"[a-z0-9']+")
# Pure interjections only; particles that carry meaning (吧 呢 嘛 呀) are kept.
FILLERS = frozenset("嗯啊呃哦额噢喔诶欸唉哎")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF
        or 0xF900 <= cp <= 0xFAFF
        or 0x3040 <= cp <= 0x30FF  # kana
        or 0xAC00 <= cp <= 0xD7AF  # hangul
    )


def normalize(text: str) -> str:
    """NFKC, lower-case, punctuation and symbols replaced by spaces."""
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        out.append(" " if cat[0] in ("P", "S", "Z", "C") else ch)
    return "".join(out)


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for chunk in normalize(text).split():
        i = 0
        while i < len(chunk):
            ch = chunk[i]
            if _is_cjk(ch):
                tokens.append(ch)
                i += 1
                continue
            m = _LATIN.match(chunk, i)
            if m:
                tokens.append(m.group())
                i = m.end()
            else:  # other scripts: one token per character
                tokens.append(ch)
                i += 1
    return tokens


@dataclass(frozen=True)
class ErrorCounts:
    ref_len: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float:
        return self.errors / self.ref_len if self.ref_len else 0.0


def edit_counts(ref: Sequence[str], hyp: Sequence[str]) -> ErrorCounts:
    """Levenshtein alignment counts (substitutions, deletions, insertions)."""
    n, m = len(ref), len(hyp)
    # Each cell holds (cost, subs, dels, ins); rows are rolled to keep memory O(m).
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [(0, 0, 0, 0)] * m
        r = ref[i - 1]
        for j in range(1, m + 1):
            d = prev[j - 1]
            best = d if r == hyp[j - 1] else (d[0] + 1, d[1] + 1, d[2], d[3])
            up = prev[j]
            if up[0] + 1 < best[0]:
                best = (up[0] + 1, up[1], up[2] + 1, up[3])
            left = cur[j - 1]
            if left[0] + 1 < best[0]:
                best = (left[0] + 1, left[1], left[2], left[3] + 1)
            cur[j] = best
        prev = cur
    _, s, d, ins = prev[m]
    return ErrorCounts(ref_len=n, substitutions=s, deletions=d, insertions=ins)


def mixed_error_rate(reference: str, hypothesis: str, *, drop_fillers: bool = False) -> ErrorCounts:
    ref, hyp = tokenize(reference), tokenize(hypothesis)
    if drop_fillers:
        ref = [t for t in ref if t not in FILLERS]
        hyp = [t for t in hyp if t not in FILLERS]
    return edit_counts(ref, hyp)


def _squash(text: str) -> str:
    return "".join(normalize(text).split())


@dataclass(frozen=True)
class TermRecall:
    expected: int
    found: int
    per_term: dict[str, tuple[int, int]]  # term -> (found, expected)

    @property
    def recall(self) -> float:
        return self.found / self.expected if self.expected else 0.0


def term_recall(reference: str, hypothesis: str, terms: Iterable[str]) -> TermRecall:
    """How many reference occurrences of each term also occur in the hypothesis.

    Matching ignores case, width, spaces and punctuation ("Jump Scare" == "jumpscare"); a term's
    hits are capped at its reference count. Terms absent from the reference are skipped.
    """
    ref, hyp = _squash(reference), _squash(hypothesis)
    per_term: dict[str, tuple[int, int]] = {}
    for term in terms:
        key = _squash(term)
        if not key:
            continue
        expected = ref.count(key)
        if expected:
            per_term[term] = (min(hyp.count(key), expected), expected)
    return TermRecall(
        expected=sum(e for _, e in per_term.values()),
        found=sum(f for f, _ in per_term.values()),
        per_term=per_term,
    )
