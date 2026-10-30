"""Inference device / dtype selection for the local models.

PyTorch's CPU wheels already dispatch matmul / conv kernels to AVX2 or AVX-512 at runtime. What is
not automatic is the dtype: on CPUs with native BF16 (AVX512_BF16 on Zen 4/5 and Cooper Lake+,
AMX on Sapphire Rapids+) bfloat16 inference is several times faster than float32 (measured 3.8-4.7x
on encoder / decoder matmul shapes on a Ryzen AI MAX+ 392). ``resolve_dtype("auto")`` picks it
when oneDNN reports BF16 support and falls back to float32 otherwise. ``ASR_LOCAL_DTYPE``
(``auto`` | ``float32`` | ``bfloat16``) overrides the choice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

DtypeName = Literal["float32", "bfloat16", "float16"]


@dataclass(frozen=True)
class CpuFeatures:
    capability: str  # torch ATen dispatch level, e.g. "AVX512", "AVX2", "DEFAULT"
    bf16: bool  # oneDNN has native bfloat16 kernels (AVX512_BF16 / AMX)


def cpu_features() -> CpuFeatures:
    import torch

    try:
        bf16 = bool(torch.ops.mkldnn._is_mkldnn_bf16_supported())
    except (AttributeError, RuntimeError):
        bf16 = False
    return CpuFeatures(capability=torch.backends.cpu.get_cpu_capability(), bf16=bf16)


def resolve_dtype(requested: str | None = None, *, device: str = "cpu") -> DtypeName:
    """``auto`` -> bfloat16 where the hardware runs it natively, else float32.

    ``None`` and ``"auto"`` both defer to ``ASR_LOCAL_DTYPE`` first (callers pass the literal
    ``"auto"`` as their default); an explicit dtype argument wins over the environment.
    """
    choice = (requested or "").strip().lower()
    if choice in ("", "auto"):
        choice = os.environ.get("ASR_LOCAL_DTYPE", "").strip().lower() or "auto"
    if choice in ("float32", "bfloat16", "float16"):
        return choice  # type: ignore[return-value]
    if choice != "auto":
        raise ValueError(f"unknown dtype {choice!r}; use auto, float32, bfloat16 or float16")
    if device.startswith("cuda"):
        import torch

        return "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
    return "bfloat16" if cpu_features().bf16 else "float32"
