"""[2] ASR with faster-whisper (CTranslate2): the English-only route (M11, docs/M11_EXTRAS.md).

Whisper emits its own word timestamps, so no forced alignment is needed; words become merge
tokens like Paraformer's. Runs int8 on CPU and float16 on CUDA. Optional: needs the ``whisper``
extra (``faster-whisper``); the model files come from the registry (``asr models download``).
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000
BEAM_SIZE = 5


def compute_type(device: str, dtype: str = "auto") -> str:
    """CTranslate2 compute type: int8 on CPU (no bf16 kernels there), float16 on CUDA.

    ``"auto"`` defers to ``ASR_LOCAL_DTYPE`` like ``resolve_dtype``; only float32 changes it.
    """
    if (dtype or "auto").strip().lower() == "auto":
        dtype = os.environ.get("ASR_LOCAL_DTYPE", "").strip().lower() or "auto"
    if dtype == "float32":
        return "float32"
    return "float16" if device == "cuda" else "int8"


def strip_punct(word: str) -> str:
    """``" Hello,"`` -> ``"Hello"``: merge looks for sentence ends *after* a token's span."""
    word = word.strip()
    start, end = 0, len(word)
    while start < end and unicodedata.category(word[start])[0] == "P":
        start += 1
    while end > start and unicodedata.category(word[end - 1])[0] == "P":
        end -= 1
    return word[start:end]


class WhisperAsr:
    def __init__(
        self, cache_dir: Path, key: str, *, device: str = "cpu", dtype: str = "auto"
    ) -> None:
        from faster_whisper import WhisperModel

        self.key = key
        self.compute_type = compute_type(device, dtype)
        self._model = WhisperModel(
            str(model_dir(cache_dir, key)), device=device, compute_type=self.compute_type
        )

    def _run(
        self, audio: np.ndarray, *, language: str | None, terms: Sequence[str]
    ) -> tuple[str, list[tuple[str, float, float]]]:
        if len(audio) < SAMPLE_RATE // 10:
            return "", []
        segments, _info = self._model.transcribe(
            audio.astype(np.float32),
            language=language,
            beam_size=BEAM_SIZE,
            word_timestamps=True,
            vad_filter=False,  # the pipeline's FSMN-VAD already cut the chunk
            condition_on_previous_text=False,  # chunks are independent; avoids repetition loops
            hotwords=", ".join(terms) or None,
        )
        text_parts: list[str] = []
        tokens: list[tuple[str, float, float]] = []
        for seg in segments:
            text_parts.append(seg.text)
            for w in seg.words or []:
                word = strip_punct(w.word)
                if word:
                    tokens.append((word, float(w.start), float(w.end)))
        return "".join(text_parts).strip(), tokens

    def transcribe(
        self,
        chunks: Sequence[np.ndarray],
        *,
        language: str | None = "en",
        terms: Sequence[str] = (),
        context: str | None = None,
    ) -> list[str]:
        del context  # faster-whisper's initial_prompt would bias style, not just vocabulary
        return [self._run(a, language=language, terms=terms)[0] for a in chunks]

    def transcribe_timed(
        self,
        chunks: Sequence[np.ndarray],
        *,
        terms: Sequence[str] = (),
        language: str | None = "en",
    ) -> list[tuple[str, list[tuple[str, float, float]]]]:
        """Text plus per-word ``(word, start, end)`` in seconds relative to each chunk."""
        return [self._run(a, language=language, terms=terms) for a in chunks]
