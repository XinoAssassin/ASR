"""Speaker mis-assignment between two diarizations (PLAN M5 acceptance: < 10 %).

Both sides are lists of ``(start, end, label)`` turns. Time is sampled on a fixed grid; only
instants where *both* sides have exactly one speaker count (overlap regions and silence are
excluded, so this measures who-spoke-when confusion, not VAD differences). Labels are matched 1:1
by the mapping that maximises agreement (exhaustive over permutations; speaker counts are small).
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from itertools import permutations

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
    best, best_map = -1, ()
    h, r = counts.shape
    # Assign each hypothesis label to a distinct reference label (or none when h > r).
    slots = list(range(r)) + [-1] * max(h - r, 0)
    for perm in set(permutations(slots, h)):
        agree = sum(counts[i, j] for i, j in enumerate(perm) if j >= 0)
        if agree > best:
            best, best_map = agree, perm
    mapping = {hyp_labels[i]: ref_labels[j] for i, j in enumerate(best_map) if j >= 0}
    return SpeakerConfusion(
        scored_seconds=scored * step,
        misassigned_seconds=(scored - max(best, 0)) * step,
        mapping=mapping,
    )
