"""[1] VAD (FSMN-VAD via funasr) and [1b] grouping of speech segments into ASR chunks."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from asr.providers.local.device import cpu_threads
from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000
Segment = tuple[float, float]  # seconds
CUT_SEARCH_S = 2.0  # a forced cut moves this far (either way) to the quietest frame
CUT_FRAME_S = 0.01  # energy frame for that search
MIN_PIECE_S = 1.0  # no forced cut leaves a piece shorter than this


def _quietest(audio: np.ndarray, lo: float, hi: float) -> float | None:
    """Centre (seconds) of the lowest-energy ``CUT_FRAME_S`` frame in ``[lo, hi]``."""
    frame = int(CUT_FRAME_S * SAMPLE_RATE)
    a, b = int(lo * SAMPLE_RATE), min(int(hi * SAMPLE_RATE), len(audio))
    n = (b - a) // frame
    if n < 1:
        return None
    x = audio[a : a + n * frame].astype(np.float64).reshape(n, frame)
    k = int(np.argmin((x * x).sum(axis=1)))
    return (a + k * frame + frame / 2) / SAMPLE_RATE


def _split_long(
    start: float, end: float, max_len: float, audio: np.ndarray | None
) -> list[Segment]:
    """Split ``[start, end)`` into the fewest pieces of at most ``max_len`` seconds.

    Cuts start on an even grid; with ``audio`` each one moves to the quietest frame within
    ``CUT_SEARCH_S`` (a pause between words rather than the middle of one), as long as every
    piece stays within ``[MIN_PIECE_S, max_len]``.
    """
    n = max(1, int(np.ceil((end - start) / max_len)))
    step = (end - start) / n
    cuts: list[float] = []
    prev = start
    for i in range(1, n):
        ideal = start + i * step
        cut_at = ideal
        if audio is not None:
            lo = max(ideal - CUT_SEARCH_S, prev + MIN_PIECE_S, end - (n - i) * max_len)
            hi = min(ideal + CUT_SEARCH_S, prev + max_len, end - MIN_PIECE_S)
            quiet = _quietest(audio, lo, hi) if lo <= hi else None
            cut_at = ideal if quiet is None else quiet
        cuts.append(cut_at)
        prev = cut_at
    bounds = [start, *cuts, end]
    return list(itertools.pairwise(bounds))


def merge_segments(
    segments: Sequence[Segment],
    *,
    max_len: float = 25.0,
    max_gap: float = 1.5,
    audio: np.ndarray | None = None,
) -> list[Segment]:
    """Group consecutive speech segments into chunks of at most ``max_len`` seconds.

    A new chunk starts when adding the next segment would exceed ``max_len`` or when the silence
    before it is longer than ``max_gap``. Segments longer than ``max_len`` are split; given the
    track's 16 kHz ``audio``, each cut lands on the quietest 10 ms frame near the even split
    point instead of mid-word (``_split_long``).
    """
    pieces: list[Segment] = []
    for start, end in segments:
        if end <= start:
            continue
        pieces.extend(_split_long(start, end, max_len, audio))
    chunks: list[Segment] = []
    for start, end in pieces:
        if chunks and end - chunks[-1][0] <= max_len and start - chunks[-1][1] <= max_gap:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))
    return chunks


def cut(audio: np.ndarray, chunks: Sequence[Segment]) -> list[np.ndarray]:
    return [audio[int(s * SAMPLE_RATE) : int(e * SAMPLE_RATE)] for s, e in chunks]


class FsmnVad:
    """FunASR FSMN-VAD loaded from the local model cache (never downloads)."""

    def __init__(self, cache_dir: Path, *, device: str = "cpu") -> None:
        from funasr import AutoModel

        self._model = AutoModel(
            model=str(model_dir(cache_dir, "fsmn-vad")),
            device=device,
            ncpu=cpu_threads(),  # funasr would otherwise reset torch to 4 threads
            disable_update=True,
            disable_pbar=True,
            log_level="ERROR",
        )

    def segments(self, audio: np.ndarray) -> list[Segment]:
        result = self._model.generate(input=audio.astype(np.float32), fs=SAMPLE_RATE)
        spans = result[0].get("value", []) if result else []
        return [(b / 1000.0, e / 1000.0) for b, e in spans]
