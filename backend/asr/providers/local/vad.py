"""[1] VAD (FSMN-VAD via funasr) and [1b] grouping of speech segments into ASR chunks."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000
Segment = tuple[float, float]  # seconds


def merge_segments(
    segments: Sequence[Segment], *, max_len: float = 25.0, max_gap: float = 1.5
) -> list[Segment]:
    """Group consecutive speech segments into chunks of at most ``max_len`` seconds.

    A new chunk starts when adding the next segment would exceed ``max_len`` or when the silence
    before it is longer than ``max_gap``. Segments longer than ``max_len`` are split evenly.
    """
    pieces: list[Segment] = []
    for start, end in segments:
        if end <= start:
            continue
        n = max(1, int(np.ceil((end - start) / max_len)))
        step = (end - start) / n
        pieces.extend((start + i * step, start + (i + 1) * step) for i in range(n))
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
            disable_update=True,
            disable_pbar=True,
            log_level="ERROR",
        )

    def segments(self, audio: np.ndarray) -> list[Segment]:
        result = self._model.generate(input=audio.astype(np.float32), fs=SAMPLE_RATE)
        spans = result[0].get("value", []) if result else []
        return [(b / 1000.0, e / 1000.0) for b, e in spans]
