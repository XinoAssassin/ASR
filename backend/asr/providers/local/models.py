"""Local model registry and explicit download (AGENTS.md rule 6).

Weights live under ``ASR_MODEL_CACHE_DIR/<hub>/<repo_id>``. Nothing here downloads implicitly:
loaders use ``model_dir()`` and fail with ``ModelNotDownloaded`` when the directory is missing;
``download()`` is only called from the explicit CLI command (and later the settings endpoint).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Hub = Literal["hf", "modelscope"]
Kind = Literal["asr", "vad", "punc", "diarize", "align"]


@dataclass(frozen=True)
class ModelSpec:
    key: str
    hub: Hub
    repo_id: str
    kind: Kind
    engine: Literal["qwen3_asr", "fun_asr_nano", "funasr"]
    marker: str  # weights file; its presence (and no partial files) means the download completed
    note: str = ""
    mirror: str | None = None  # same files on ModelScope (faster from mainland China)


MODELS: dict[str, ModelSpec] = {
    spec.key: spec
    for spec in (
        ModelSpec(
            "qwen3-asr-0.6b",
            "hf",
            "Qwen/Qwen3-ASR-0.6B-hf",
            "asr",
            "qwen3_asr",
            "model.safetensors",
            "Qwen3-ASR 0.6B (default on CPUs without native BF16)",
        ),
        ModelSpec(
            "qwen3-asr-1.7b",
            "hf",
            "Qwen/Qwen3-ASR-1.7B-hf",
            "asr",
            "qwen3_asr",
            "model.safetensors",
            "Qwen3-ASR 1.7B (default on CUDA and CPUs with native BF16)",
            mirror="Qwen/Qwen3-ASR-1.7B-hf",
        ),
        ModelSpec(
            "fun-asr-nano",
            "hf",
            "FunAudioLLM/Fun-ASR-Nano-2512-hf",
            "asr",
            "fun_asr_nano",
            "model.safetensors",
            "Fun-ASR-Nano-2512 (optional; dominated by Qwen3-0.6B in M4)",
        ),
        ModelSpec(
            "seaco-paraformer",
            "modelscope",
            "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            "asr",
            "funasr",
            "model.pt",
            "SeACo-Paraformer large (fast draft mode, fp32 only)",
        ),
        ModelSpec(
            "qwen3-forced-aligner",
            "hf",
            "Qwen/Qwen3-ForcedAligner-0.6B-hf",
            "align",
            "qwen3_asr",
            "model.safetensors",
            "Qwen3-ForcedAligner 0.6B (word timestamps, <= 5 min per call)",
            mirror="Qwen/Qwen3-ForcedAligner-0.6B-hf",
        ),
        ModelSpec(
            "campplus",
            "modelscope",
            "iic/speech_campplus_sv_zh-cn_16k-common",
            "diarize",
            "funasr",
            "campplus_cn_common.bin",
            "CAM++ speaker embeddings (diarization)",
        ),
        ModelSpec(
            "fsmn-vad",
            "modelscope",
            "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            "vad",
            "funasr",
            "model.pt",
            "FSMN-VAD",
        ),
    )
}


class ModelNotDownloaded(RuntimeError):
    """The model directory is missing; run ``asr models download <key>`` first."""


def spec(key: str) -> ModelSpec:
    try:
        return MODELS[key]
    except KeyError:
        raise KeyError(f"unknown model {key!r}; known: {', '.join(MODELS)}") from None


def model_path(cache_dir: Path, key: str) -> Path:
    s = spec(key)
    return Path(cache_dir) / s.hub / s.repo_id


_PARTIAL_SUFFIXES = (".incomplete", ".____temp", "._____temp")


def is_downloaded(cache_dir: Path, key: str) -> bool:
    """Weights present and no partial files left by an interrupted hub download."""
    path = model_path(cache_dir, key)
    if not (path / spec(key).marker).is_file():
        return False
    return not any(p.name.endswith(_PARTIAL_SUFFIXES) for p in path.rglob("*"))


def model_dir(cache_dir: Path, key: str) -> Path:
    """Directory of a downloaded model; never downloads."""
    if not is_downloaded(cache_dir, key):
        raise ModelNotDownloaded(
            f"model {key!r} is not in {cache_dir}; run `asr models download {key}`"
        )
    return model_path(cache_dir, key)


def download(cache_dir: Path, key: str, *, mirror: bool = False) -> Path:
    """Fetch a model into the cache (explicit user action only).

    ``mirror=True`` fetches an HF model from its ModelScope mirror instead; the files land in the
    same directory, so loaders do not care where they came from.
    """
    s = spec(key)
    target = model_path(cache_dir, key)
    target.mkdir(parents=True, exist_ok=True)
    if mirror and not s.mirror:
        raise ValueError(f"model {key!r} has no ModelScope mirror")
    if s.hub == "hf" and not mirror:
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=s.repo_id, local_dir=str(target))
    else:
        from modelscope import snapshot_download as ms_snapshot_download

        # Partial files of an interrupted HF download would mark the model incomplete forever.
        shutil.rmtree(target / ".cache" / "huggingface", ignore_errors=True)
        ms_snapshot_download(s.mirror if mirror else s.repo_id, local_dir=str(target))
    if not (target / s.marker).is_file():
        raise RuntimeError(f"download of {key} finished without {s.marker}")
    # The hub reported success, so any partial file left is a stale leftover of an earlier,
    # interrupted transfer (e.g. a stalled Xet download that a retry finished over plain HTTPS).
    for partial in [p for p in target.rglob("*") if p.name.endswith(_PARTIAL_SUFFIXES)]:
        partial.unlink(missing_ok=True)
    return target
