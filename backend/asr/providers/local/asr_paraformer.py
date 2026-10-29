"""[2] ASR with SeACo-Paraformer (funasr SDK): fast non-autoregressive Chinese model, hotwords."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000


class ParaformerAsr:
    key = "seaco-paraformer"

    def __init__(self, cache_dir: Path, *, device: str = "cpu", dtype: str = "auto") -> None:
        from funasr import AutoModel

        # Always float32: the CIF predictor accumulates per-frame weights into token boundaries,
        # and in bfloat16 that sum drifts on long chunks until it emits fewer timestamps than
        # tokens (funasr then fails with IndexError in sentence_postprocess). The model is already
        # ~30x faster than real time on CPU, so bf16 would gain little.
        if dtype not in ("auto", "float32"):
            raise ValueError("SeACo-Paraformer runs in float32 only (CIF precision)")
        self.dtype = "float32"

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
            result = self._model.generate(**kwargs)
            texts.append(result[0].get("text", "").strip() if result else "")
        return texts
