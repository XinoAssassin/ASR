"""Inference device / dtype selection for the local models.

PyTorch's CPU wheels already dispatch matmul / conv kernels to AVX2 or AVX-512 at runtime. What is
not automatic is the dtype: on CPUs with native BF16 (AVX512_BF16 on Zen 4/5 and Cooper Lake+,
AMX on Sapphire Rapids+) bfloat16 inference is several times faster than float32 (measured 3.8-4.7x
on encoder / decoder matmul shapes on a Ryzen AI MAX+ 392). ``resolve_dtype("auto")`` picks it
when oneDNN reports BF16 support and falls back to float32 otherwise. ``ASR_LOCAL_DTYPE``
(``auto`` | ``float32`` | ``bfloat16``) overrides the choice.

GPU devices: ``cuda`` runs on torch's CUDA device. ``rocm`` is the AMD alias: ROCm builds of torch
expose HIP GPUs through the ``torch.cuda`` API (``torch.version.hip`` is set), so ``rocm`` maps to
torch's ``cuda`` device once ``torch_device`` has checked that the installed torch is a ROCm build.
``cuda`` works on a ROCm build too. CTranslate2 (faster-whisper) has no ROCm build, so the Whisper
engine falls back to the CPU there (``whisper_device``).
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from typing import Any, Literal

DtypeName = Literal["float32", "bfloat16", "float16"]
Device = Literal["cpu", "cuda", "rocm"]
GpuBackend = Literal["cuda", "rocm"]
GPU_DEVICES: tuple[GpuBackend, GpuBackend] = ("cuda", "rocm")

ROCM_HINT = (
    "on Linux pass /dev/kfd and /dev/dri to the container and add the host's render and video "
    "groups, on Windows install an AMD Adrenalin driver with ROCm support; a GPU outside the ROCm "
    "support list may need HSA_OVERRIDE_GFX_VERSION (docs/DEPLOYMENT.md §8)"
)
CUDA_HINT = "check the NVIDIA driver and that the GPUs are passed to the container"


class DeviceError(ValueError):
    """The requested device cannot run with the installed torch build / visible hardware."""


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


def cpu_threads() -> int:
    """CPU inference threads: ``ASR_TORCH_THREADS``, default min(16, CPU count) (M4 setting).

    Also handed to funasr's ``AutoModel(ncpu=...)``: without it funasr resets torch to 4 threads
    when a model is built and again on every ``generate`` call.
    """
    raw = os.environ.get("ASR_TORCH_THREADS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else min(16, os.cpu_count() or 4)


def gpu_backend() -> GpuBackend | None:
    """GPU backend the installed torch was built for: ``rocm``, ``cuda`` or None (CPU-only)."""
    import torch

    if getattr(torch.version, "hip", None):
        return "rocm"
    if getattr(torch.version, "cuda", None):
        return "cuda"
    return None


def torch_device(device: str) -> str:
    """Configured device (``cpu`` | ``cuda`` | ``rocm``) -> torch device string.

    Raises ``DeviceError`` with an actionable message when the installed torch cannot serve the
    request: ``rocm`` on a CUDA / CPU build, ``cuda`` on a CPU-only build, or no visible GPU.
    """
    name = (device or "cpu").strip().lower()
    if name == "cpu":
        return "cpu"
    if name not in GPU_DEVICES:
        raise DeviceError(f"unknown device {device!r}; use cpu, cuda or rocm")
    import torch

    backend = gpu_backend()
    build = f"torch {torch.__version__} ({backend or 'CPU-only'} build)"
    if name == "rocm" and backend != "rocm":
        raise DeviceError(
            f"device 'rocm' needs a ROCm build of torch, but {build} is installed "
            "(use the backend-rocm image, or uv sync --extra rocm --extra local)"
        )
    if backend is None:
        raise DeviceError(
            f"device 'cuda' needs a CUDA build of torch, but {build} is installed "
            "(use the backend-cuda image, or uv sync --extra cuda --extra local)"
        )
    if not torch.cuda.is_available():
        hint = ROCM_HINT if backend == "rocm" else CUDA_HINT
        raise DeviceError(f"device {name!r}: {build} sees no GPU; {hint}")
    return "cuda"


def disable_broken_rocm_kernels() -> list[str]:
    """ROCm only: switch off kernel libraries that fail on this GPU / platform; returns their names.

    Found on gfx1151 with AMD's Windows build (torch 2.14.0+rocm10.1.0), where the failures raise
    instead of falling back:

    - flash / memory-efficient SDPA (AOTriton) have no code object for the GPU ("device kernel
      image is invalid"), which breaks every transformers model using SDPA;
    - MIOpen compiles its batch-norm / conv kernels at run time with hipRTC, which cannot find
      the C++ headers there ("'type_traits' file not found"), which breaks CAM++.

    Each is tried once on a tiny input and disabled when it fails: attention then uses the math
    kernel and convolution / batch norm torch's native HIP kernels. CUDA builds are left alone.
    """
    import torch

    if not getattr(torch.version, "hip", None):
        return []
    import warnings

    from torch.nn.attention import SDPBackend, sdpa_kernel

    q = torch.zeros(1, 1, 16, 64, device="cuda", dtype=torch.bfloat16)
    disabled = []
    for backend, enable in (
        (SDPBackend.FLASH_ATTENTION, torch.backends.cuda.enable_flash_sdp),
        (SDPBackend.EFFICIENT_ATTENTION, torch.backends.cuda.enable_mem_efficient_sdp),
    ):
        try:
            with warnings.catch_warnings(), sdpa_kernel([backend]):
                warnings.simplefilter("ignore")
                torch.nn.functional.scaled_dot_product_attention(q, q, q)
                torch.cuda.synchronize()
        except Exception:  # hipErrorInvalidImage / "No available kernel"
            enable(False)
            disabled.append(backend.name.lower())
    x = torch.ones(2, 4, 8, 8, device="cuda")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            zeros, ones = torch.zeros(4, device="cuda"), torch.ones(4, device="cuda")
            # weight and bias must be set, or torch skips MIOpen for batch norm
            torch.nn.functional.batch_norm(x, zeros, ones, ones, zeros, training=False)
            torch.nn.functional.conv2d(x, torch.ones(4, 4, 3, 3, device="cuda"))
            torch.cuda.synchronize()
    except Exception:  # miopenStatusUnknownError: run-time kernel compilation failed
        torch.backends.cudnn.enabled = False  # MIOpen sits behind the cudnn switch on ROCm
        disabled.append("miopen")
    return disabled


def whisper_device(device: str) -> tuple[str, str | None]:
    """CTranslate2 device for faster-whisper, plus a warning when it has to fall back.

    CTranslate2 ships CPU and CUDA builds only, so on ROCm (``rocm``, or ``cuda`` on a ROCm torch)
    the Whisper model runs on the CPU while VAD and diarization stay on the GPU.
    """
    name = (device or "cpu").strip().lower()
    if name == "rocm" or (name == "cuda" and gpu_backend() == "rocm"):
        return "cpu", (
            "faster-whisper (CTranslate2) has no ROCm build: the Whisper model ran on the CPU"
        )
    return ("cuda" if name == "cuda" else "cpu"), None


def cpu_name() -> str | None:
    """CPU model name, e.g. "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S". No torch, no subprocess."""
    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            with key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or None
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip() or None
    except OSError:
        pass
    return platform.processor() or None


@dataclass(frozen=True)
class DeviceStatus:
    id: Device
    available: bool
    name: str | None  # CPU model / GPU name
    detail: str | None  # why it is unavailable, or extra facts (arch, runtime) when it is


@dataclass(frozen=True)
class HardwareReport:
    devices: tuple[DeviceStatus, ...]  # always cpu, cuda, rocm in that order
    default: Device  # the GPU torch can use, else cpu


def _gpu_names(torch: Any) -> list[tuple[str, str]]:
    names = []
    for index in range(int(torch.cuda.device_count())):
        arch = getattr(torch.cuda.get_device_properties(index), "gcnArchName", "") or ""
        names.append((str(torch.cuda.get_device_name(index)), str(arch).split(":")[0]))
    return names


def hardware_report() -> HardwareReport:
    """Which of cpu / cuda / rocm the installed torch can run on (Settings, job validation).

    Imports torch (seconds on first call), never downloads anything and never raises. ``cuda``
    is available on a CUDA build with a visible GPU, ``rocm`` on a ROCm (HIP) build with one.
    """
    cpu = DeviceStatus("cpu", True, cpu_name(), f"{os.cpu_count() or '?'} logical cores")
    try:
        import torch
    except ImportError:
        missing = "torch is not installed (install the backend with the local extra)"
        return HardwareReport(
            (
                cpu,
                DeviceStatus("cuda", False, None, missing),
                DeviceStatus("rocm", False, None, missing),
            ),
            "cpu",
        )
    backend = gpu_backend()
    try:
        gpus = _gpu_names(torch) if backend and torch.cuda.is_available() else []
        error = None
    except Exception as exc:  # a broken driver must not take the report down
        gpus, error = [], f"GPU probe failed: {exc}"
    build = {"rocm": "a ROCm build", "cuda": "a CUDA build", None: "a CPU build"}[backend]
    statuses = [cpu]
    for want in GPU_DEVICES:
        if backend != want:
            detail = f"torch {torch.__version__} is {build}"
            if want == "rocm":
                detail += " (install the rocm extra)"
            elif backend == "rocm":
                detail += " (AMD GPUs run as rocm)"
            else:
                detail += " (install the cuda extra)"
            statuses.append(DeviceStatus(want, False, None, detail))
        elif not gpus:
            hint = ROCM_HINT if want == "rocm" else CUDA_HINT
            statuses.append(
                DeviceStatus(
                    want, False, None, error or f"torch {torch.__version__} sees no GPU; {hint}"
                )
            )
        else:
            runtime = torch.version.hip if want == "rocm" else torch.version.cuda
            arch = ", ".join(sorted({a for _, a in gpus if a}))
            facts = [
                f"{'HIP' if want == 'rocm' else 'CUDA'} {runtime}",
                f"torch {torch.__version__}",
            ]
            if arch:
                facts.insert(0, arch)
            if len(gpus) > 1:
                facts.append(f"{len(gpus)} GPUs")
            statuses.append(DeviceStatus(want, True, gpus[0][0], ", ".join(facts)))
    default: Device = next((d.id for d in statuses[1:] if d.available), "cpu")
    return HardwareReport(tuple(statuses), default)


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
    if device.startswith(GPU_DEVICES):
        # ROCm builds answer through torch.cuda as well (True on every HIP GPU).
        import torch

        return "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
    return "bfloat16" if cpu_features().bf16 else "float32"
