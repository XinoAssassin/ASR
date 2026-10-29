"""[2] ASR with SeACo-Paraformer (funasr SDK): fast non-autoregressive Chinese model, hotwords."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from asr.providers.local.device import resolve_dtype
from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000


class ParaformerAsr:
    key = "seaco-paraformer"

    def __init__(self, cache_dir: Path, *, device: str = "cpu", dtype: str = "auto") -> None:
        import torch
        from funasr import AutoModel

        self.dtype = resolve_dtype(dtype, device=device)
        # Weights stay float32; bfloat16 runs under autocast so the fbank frontend keeps full
        # precision while Linear / Conv layers use the native BF16 (AVX512_BF16 / AMX) kernels.
        self._autocast = (
            torch.autocast(device_type=device.split(":")[0], dtype=torch.bfloat16)
            if self.dtype == "bfloat16"
            else contextlib.nullcontext()
        )

        self._model = AutoModel(
            model=str(model_dir(cache_dir, self.key)),
            device=device,
            disable_update=True,
            disable_pbar=True,
            log_level="ERROR",
        )

    def transcribe(
        self,
        chunks: Sequence[np.ndarray],
        *,
        language: str | None = "zh",
        terms: Sequence[str] = (),
        context: str | None = None,
    ) -> list[str]:
        del language, context  # Chinese-only model without a context input
        hotword = " ".join(t.replace(" ", "") for t in terms) or None
        texts: list[str] = []
        for audio in chunks:
            if len(audio) < SAMPLE_RATE // 10:
                texts.append("")
                continue
            kwargs: dict[str, object] = {"input": audio.astype(np.float32), "fs": SAMPLE_RATE}
            if hotword:
                kwargs["hotword"] = hotword
            with self._autocast:
                result = self._model.generate(**kwargs)
            texts.append(result[0].get("text", "").strip() if result else "")
        return texts
