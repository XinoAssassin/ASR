"""[2] ASR with native transformers models: Qwen3-ASR (0.6B / 1.7B) and Fun-ASR-Nano-2512.

Terms reach Qwen3-ASR through its system prompt (``context``) and Fun-ASR-Nano through its native
``keywords``; term weights only matter to the cloud vocabulary and are ignored here.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

from asr.providers.local.device import resolve_dtype
from asr.providers.local.models import model_dir, spec

SAMPLE_RATE = 16_000


def qwen3_prompt(terms: Sequence[str], context: str | None) -> str | None:
    parts = []
    if context:
        parts.append(context.strip())
    if terms:
        parts.append("术语: " + ", ".join(terms))
    return "\n".join(parts) or None


class TransformersAsr:
    def __init__(
        self, cache_dir: Path, key: str, *, device: str = "cpu", dtype: str = "auto"
    ) -> None:
        import torch
        from transformers import AutoModelForMultimodalLM, AutoModelForSpeechSeq2Seq, AutoProcessor

        self.key = key
        self.engine = spec(key).engine
        if self.engine not in ("qwen3_asr", "fun_asr_nano"):
            raise ValueError(f"{key} is not a transformers ASR model")
        path = str(model_dir(cache_dir, key))
        self._torch = torch
        self.dtype = resolve_dtype(dtype, device=device)
        self._dtype = getattr(torch, self.dtype)
        self.processor = AutoProcessor.from_pretrained(path, local_files_only=True)
        auto = AutoModelForMultimodalLM if self.engine == "qwen3_asr" else AutoModelForSpeechSeq2Seq
        self.model = (
            auto.from_pretrained(path, local_files_only=True, dtype=self._dtype).to(device).eval()
        )
        self.device = device

    def transcribe(
        self,
        chunks: Sequence[np.ndarray],
        *,
        language: str | None = "zh",
        terms: Sequence[str] = (),
        context: str | None = None,
        max_new_tokens: int = 448,
    ) -> list[str]:
        texts: list[str] = []
        for audio in chunks:
            if len(audio) < SAMPLE_RATE // 10:  # < 100 ms: nothing to recognise
                texts.append("")
                continue
            kwargs: dict[str, object] = {
                "audio": audio.astype(np.float32),
                "language": language,
                "processor_kwargs": {
                    "return_tensors": "pt",
                    "audio_kwargs": {"sampling_rate": SAMPLE_RATE},
                },
            }
            if self.engine == "qwen3_asr":
                kwargs["prompt"] = qwen3_prompt(terms, context)
            else:
                kwargs["prompt"] = context or None
                kwargs["keywords"] = list(terms) or None
            inputs = self.processor.apply_transcription_request(**kwargs)
            inputs = inputs.to(self.device)
            if "input_features" in inputs:
                inputs["input_features"] = inputs["input_features"].to(self._dtype)
            with self._torch.inference_mode():
                out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
            new_tokens = out[:, inputs["input_ids"].shape[1] :]
            if self.engine == "qwen3_asr":
                text = self.processor.decode(new_tokens, return_format="transcription_only")[0]
            else:
                text = self.processor.batch_decode(new_tokens, skip_special_tokens=True)[0]
            texts.append(text.strip())
        return texts
