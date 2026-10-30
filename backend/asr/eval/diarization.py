"""Speaker mis-assignment between two diarizations (PLAN M5 acceptance: < 10 %).

Both sides are lists of ``(start, end, label)`` turns. Time is sampled on a fixed grid; only
instants where *both* sides have exactly one speaker count (overlap regions and silence are
excluded, so this measures who-spoke-when confusion, not VAD differences). Labels are matched 1:1
by the mapping that maximises agreement (an optimal assignment: scipy's Hungarian solver when
available, else an exact dynamic programme over label subsets).
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np

TurnLike = tuple[float, float, Hashable]


@dataclass(frozen=True)
class SpeakerConfusion:
    scored_seconds: float
    misassigned_seconds: float
    mapping: dict[Hashable, Hashable]  # hypothesis label -> reference label

    @property
    def rate(self) -> float:
        return self.misassigned_seconds / self.scored_seconds if self.scored_seconds else 0.0


def _frames(turns: Sequence[TurnLike], n: int, step: float) -> tuple[np.ndarray, list[Hashable]]:
    """Per-frame label index; -1 = silence, -2 = overlapping speakers."""
    labels = sorted({t[2] for t in turns}, key=str)
    index = {lab: i for i, lab in enumerate(labels)}
    grid = np.full(n, -1, dtype=np.int32)
    for start, end, lab in turns:
        a, b = max(round(start / step), 0), min(round(end / step), n)
        if b <= a:
            continue
        seg = grid[a:b]
        seg[(seg != -1) & (seg != index[lab])] = -2
        seg[seg == -1] = index[lab]
    return grid, labels


def speaker_confusion(
    reference: Sequence[TurnLike], hypothesis: Sequence[TurnLike], *, step: float = 0.01
) -> SpeakerConfusion:
    if not reference or not hypothesis:
        return SpeakerConfusion(0.0, 0.0, {})
    horizon = max(max(t[1] for t in reference), max(t[1] for t in hypothesis))
    n = int(np.ceil(horizon / step)) + 1
    ref, ref_labels = _frames(reference, n, step)
    hyp, hyp_labels = _frames(hypothesis, n, step)
    both = (ref >= 0) & (hyp >= 0)
    counts = np.zeros((len(hyp_labels), len(ref_labels)), dtype=np.int64)
    np.add.at(counts, (hyp[both], ref[both]), 1)
    scored = int(both.sum())
    pairs = best_assignment(counts)
    best = sum(int(counts[i, j]) for i, j in pairs)
    mapping = {hyp_labels[i]: ref_labels[j] for i, j in pairs}
    return SpeakerConfusion(
        scored_seconds=scored * step,
        misassigned_seconds=(scored - best) * step,
        mapping=mapping,
    )


def best_assignment(counts: np.ndarray) -> list[tuple[int, int]]:
    """Row -> column pairs (each used at most once) maximising the summed ``counts``."""
    h, r = counts.shape
    if not h or not r:
        return []
    try:
        from scipy.optimize import linear_sum_assignment
    except ImportError:
        return _assignment_dp(counts)
    rows, cols = linear_sum_assignment(counts, maximize=True)
    return [(int(i), int(j)) for i, j in zip(rows, cols, strict=True) if counts[i, j] > 0]


def _assignment_dp(counts: np.ndarray) -> list[tuple[int, int]]:
    """Exact assignment by DP over subsets of the smaller side (fine up to ~16 labels)."""
    transpose = counts.shape[0] < counts.shape[1]
    m = counts.T if transpose else counts  # rows >= cols; DP over column subsets
    rows, cols = m.shape
    # best[mask] after processing some rows: (score, pairs)
    best: dict[int, tuple[int, list[tuple[int, int]]]] = {0: (0, [])}
    for i in range(rows):
        nxt = dict(best)
        for mask, (score, pairs) in best.items():
            for j in range(cols):
                if mask & (1 << j) or m[i, j] <= 0:
                    continue
                cand = (score + int(m[i, j]), [*pairs, (i, j)])
                key = mask | (1 << j)
                if key not in nxt or cand[0] > nxt[key][0]:
                    nxt[key] = cand
        best = nxt
    _, pairs = max(best.values(), key=lambda v: v[0])
    return [(j, i) for i, j in pairs] if transpose else pairs
