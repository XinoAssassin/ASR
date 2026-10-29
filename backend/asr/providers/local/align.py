"""[3] Forced alignment with Qwen3-ForcedAligner-0.6B (native transformers).

Gives per-token timestamps for a known transcript: CJK characters one by one, Latin words whole,
punctuation dropped. One call handles up to 5 minutes of audio; the pipeline aligns each <= 25 s
ASR chunk separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from asr.providers.local.device import resolve_dtype
from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class TimedToken:
    text: str
    start: float  # seconds, relative to the aligned audio
    end: float


class ForcedAligner:
    key = "qwen3-forced-aligner"

    def __init__(self, cache_dir: Path, *, device: str = "cpu", dtype: str = "auto") -> None:
        import torch
        from transformers import AutoModelForTokenClassification, AutoProcessor

        path = str(model_dir(cache_dir, self.key))
        self.dtype = resolve_dtype(dtype, device=device)
        self._torch = torch
        self._dtype = getattr(torch, self.dtype)
        self.processor = AutoProcessor.from_pretrained(path, local_files_only=True)
        self.model = (
            AutoModelForTokenClassification.from_pretrained(
                path, local_files_only=True, dtype=self._dtype
            )
            .to(device)
            .eval()
        )
        self.device = device

    def align(self, audio: np.ndarray, text: str, *, language: str = "zh") -> list[TimedToken]:
        if not text.strip() or len(audio) < SAMPLE_RATE // 10:
            return []
        inputs, word_lists = self.processor.prepare_forced_aligner_inputs(
            audio=audio.astype(np.float32),
            transcript=text,
            language=language,
            processor_kwargs={
                "return_tensors": "pt",
                "audio_kwargs": {"sampling_rate": SAMPLE_RATE},
            },
        )
        if not word_lists[0]:
            return []
        inputs = inputs.to(self.device)
        if "input_features" in inputs:
            inputs["input_features"] = inputs["input_features"].to(self._dtype)
        with self._torch.inference_mode():
            logits = self.model(**inputs).logits
        stamps = self.processor.decode_forced_alignment(
            logits=logits.float(),
            input_ids=inputs["input_ids"],
            word_lists=word_lists,
            timestamp_token_id=self.model.config.timestamp_token_id,
        )[0]
        duration = len(audio) / SAMPLE_RATE
        return [
            TimedToken(
                s["text"],
                min(float(s["start_time"]), duration),
                min(float(s["end_time"]), duration),
            )
            for s in stamps
        ]
