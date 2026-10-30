"""ROCm (AMD GPU) device handling with a fake ``torch`` module: no real GPU, no torch import (D9).

ROCm builds of torch expose HIP GPUs through ``torch.cuda`` and set ``torch.version.hip``; the
fakes below model a ROCm build, a CUDA build and a CPU-only build.
"""

from __future__ import annotations

import contextlib
import enum
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from asr.providers.base import AudioSource, JobOptions, LocalOptions, ProviderError
from asr.providers.local import pipeline as pipeline_mod
from asr.providers.local.device import (
    DeviceError,
    DeviceStatus,
    HardwareReport,
    cpu_threads,
    disable_broken_rocm_kernels,
    hardware_report,
    resolve_dtype,
    torch_device,
    whisper_device,
)
from server.config import EnvSettings, effective_settings
from tests.conftest import EnvFactory
from tests.fixtures.dashscope.support import Recorder
from tests.test_audio_fixtures import write_audio
from tests.test_jobs import FakeProvider, add_upload, install
from tests.test_local_pipeline import FakeDiarizer, FakeVad
from tests.test_routes_jobs import _fake_models

WHISPER = "faster-whisper-small"


def fake_torch(
    backend: str | None,
    *,
    available: bool = True,
    bf16: bool = True,
    devices: tuple[tuple[str, str], ...] = (("AMD Radeon 8060S", "gfx1151:sramecc-:xnack-"),),
    broken: tuple[str, ...] = (),
) -> types.ModuleType:
    torch = types.ModuleType("torch")
    torch.__version__ = {"rocm": "2.14.1+rocm7.2", "cuda": "2.14.1+cu126"}.get(  # type: ignore[attr-defined]
        backend or "", "2.14.1+cpu"
    )
    torch.version = types.SimpleNamespace(  # type: ignore[attr-defined]
        hip="7.2.53211" if backend == "rocm" else None,
        cuda="12.6" if backend == "cuda" else None,
    )
    visible = devices if backend and available else ()
    torch.cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        is_available=lambda: bool(backend) and available,
        is_bf16_supported=lambda: bf16,
        device_count=lambda: len(visible),
        get_device_name=lambda i: visible[i][0],
        get_device_properties=lambda i: types.SimpleNamespace(gcnArchName=visible[i][1]),
    )
    torch.set_num_threads = lambda n: None  # type: ignore[attr-defined]
    torch.cuda.synchronize = lambda: None
    torch.bfloat16 = "bfloat16"  # type: ignore[attr-defined]
    torch.zeros = torch.ones = lambda *a, **k: object()  # type: ignore[attr-defined]

    # Kernel libraries for disable_broken_rocm_kernels: names in `broken` raise when used.
    torch.broken = set(broken)  # type: ignore[attr-defined]
    torch.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
        cuda=types.SimpleNamespace(flash=True, efficient=True),
        cudnn=types.SimpleNamespace(enabled=True),
    )
    cuda_backends = torch.backends.cuda
    cuda_backends.enable_flash_sdp = lambda on: setattr(cuda_backends, "flash", on)
    cuda_backends.enable_mem_efficient_sdp = lambda on: setattr(cuda_backends, "efficient", on)
    attention = types.ModuleType("torch.nn.attention")
    attention.SDPBackend = enum.Enum(  # type: ignore[attr-defined]
        "SDPBackend", ["FLASH_ATTENTION", "EFFICIENT_ATTENTION", "MATH"]
    )
    active: list[str] = []

    @contextlib.contextmanager
    def sdpa_kernel(backends):
        active[:] = [b.name.lower() for b in backends]
        yield

    def check(*names: str) -> None:
        if torch.broken.intersection(names):  # type: ignore[attr-defined]
            raise RuntimeError("HIP error: device kernel image is invalid")

    attention.sdpa_kernel = sdpa_kernel  # type: ignore[attr-defined]
    functional = types.ModuleType("torch.nn.functional")
    functional.scaled_dot_product_attention = lambda *a, **k: check(*active)  # type: ignore[attr-defined]
    functional.batch_norm = lambda *a, **k: check("miopen")  # type: ignore[attr-defined]
    functional.conv2d = lambda *a, **k: check("miopen")  # type: ignore[attr-defined]
    nn = types.ModuleType("torch.nn")
    nn.attention, nn.functional = attention, functional  # type: ignore[attr-defined]
    torch.nn = nn  # type: ignore[attr-defined]
    torch.__path__ = []  # type: ignore[attr-defined]  # a package, so `torch.nn.attention` imports
    return torch


@pytest.fixture
def use_torch(monkeypatch: pytest.MonkeyPatch):
    def install_fake(backend: str | None, **kwargs: Any) -> types.ModuleType:
        torch = fake_torch(backend, **kwargs)
        monkeypatch.setitem(sys.modules, "torch", torch)
        monkeypatch.setitem(sys.modules, "torch.nn", torch.nn)
        monkeypatch.setitem(sys.modules, "torch.nn.attention", torch.nn.attention)
        monkeypatch.setitem(sys.modules, "torch.nn.functional", torch.nn.functional)
        return torch

    return install_fake


@pytest.fixture
def no_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``import torch`` raises: proves a code path never touches torch."""
    monkeypatch.setitem(sys.modules, "torch", None)


# --------------------------------------------------------------------------------- torch_device
def test_rocm_maps_to_torch_cuda_on_a_rocm_build(use_torch) -> None:
    use_torch("rocm")
    assert torch_device("rocm") == "cuda"
    assert torch_device(" ROCm ") == "cuda"
    assert torch_device("cuda") == "cuda"  # HIP answers through torch.cuda as well


def test_cpu_never_imports_torch(no_torch) -> None:
    assert torch_device("cpu") == "cpu"
    assert whisper_device("cpu") == ("cpu", None)


@pytest.mark.parametrize("backend", ["cuda", None])
def test_rocm_needs_a_rocm_build(use_torch, backend: str | None) -> None:
    use_torch(backend)
    with pytest.raises(DeviceError, match="needs a ROCm build of torch") as info:
        torch_device("rocm")
    assert "backend-rocm" in str(info.value) and "--extra rocm" in str(info.value)
    assert ("CPU-only" in str(info.value)) is (backend is None)


def test_cuda_needs_a_gpu_build(use_torch) -> None:
    use_torch(None)
    with pytest.raises(DeviceError, match=r"CUDA build of torch.*CPU-only"):
        torch_device("cuda")


def test_rocm_without_visible_gpu_names_the_usual_causes(use_torch) -> None:
    use_torch("rocm", available=False)
    with pytest.raises(DeviceError, match="sees no GPU") as info:
        torch_device("rocm")
    assert "/dev/kfd" in str(info.value) and "HSA_OVERRIDE_GFX_VERSION" in str(info.value)
    use_torch("cuda", available=False)
    with pytest.raises(DeviceError, match="NVIDIA driver"):
        torch_device("cuda")


def test_unknown_device(no_torch) -> None:
    with pytest.raises(DeviceError, match="use cpu, cuda or rocm"):
        torch_device("tpu")


# ---------------------------------------------------------------------------------------- dtype
def test_rocm_dtype_follows_torch_cuda_bf16(use_torch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASR_LOCAL_DTYPE", raising=False)
    use_torch("rocm", bf16=True)
    assert resolve_dtype("auto", device="rocm") == "bfloat16"
    assert pipeline_mod.default_asr_model("rocm") == "qwen3-asr-1.7b"
    use_torch("rocm", bf16=False)
    assert resolve_dtype("auto", device="rocm") == "float16"
    assert pipeline_mod.default_asr_model("rocm") == "qwen3-asr-0.6b"
    monkeypatch.setenv("ASR_LOCAL_DTYPE", "float32")
    assert resolve_dtype("auto", device="rocm") == "float32"


# -------------------------------------------------------------------------------------- whisper
def test_whisper_falls_back_to_cpu_on_rocm(use_torch) -> None:
    use_torch("rocm")
    for configured in ("rocm", "cuda"):
        on, warning = whisper_device(configured)
        assert on == "cpu" and warning is not None and "no ROCm build" in warning
    use_torch("cuda")
    assert whisper_device("cuda") == ("cuda", None)


# ------------------------------------------------------------------------------ hardware_report
def _by_id(report: HardwareReport) -> dict[str, DeviceStatus]:
    assert [d.id for d in report.devices] == ["cpu", "cuda", "rocm"]
    return {d.id: d for d in report.devices}


def test_hardware_report_on_rocm(use_torch, monkeypatch: pytest.MonkeyPatch) -> None:
    from asr.providers.local import device

    monkeypatch.setattr(device, "cpu_name", lambda: "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S")
    use_torch("rocm", devices=(("AMD Radeon(TM) 8060S Graphics", "gfx1151:sramecc-:xnack-"),))
    report = hardware_report()
    devices = _by_id(report)
    assert report.default == "rocm"
    assert (
        devices["cpu"].available and devices["cpu"].name == "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S"
    )
    assert devices["rocm"] == DeviceStatus(
        "rocm",
        True,
        "AMD Radeon(TM) 8060S Graphics",
        "gfx1151, HIP 7.2.53211, torch 2.14.1+rocm7.2",
    )
    cuda = devices["cuda"]
    assert not cuda.available and cuda.name is None
    assert cuda.detail is not None and "ROCm build" in cuda.detail and "rocm" in cuda.detail


def test_hardware_report_on_cuda_and_cpu_builds(use_torch) -> None:
    use_torch("cuda", devices=(("NVIDIA GeForce RTX 4090", ""), ("NVIDIA GeForce RTX 4090", "")))
    report = hardware_report()
    devices = _by_id(report)
    assert report.default == "cuda"
    assert devices["cuda"].available and devices["cuda"].name == "NVIDIA GeForce RTX 4090"
    assert devices["cuda"].detail == "CUDA 12.6, torch 2.14.1+cu126, 2 GPUs"
    assert not devices["rocm"].available and "CUDA build" in (devices["rocm"].detail or "")

    use_torch(None)
    report = hardware_report()
    devices = _by_id(report)
    assert report.default == "cpu"
    for gpu in ("cuda", "rocm"):
        assert not devices[gpu].available
        assert "is a CPU build" in (devices[gpu].detail or "")


def test_hardware_report_without_visible_gpu(use_torch) -> None:
    use_torch("rocm", available=False)
    report = hardware_report()
    rocm = _by_id(report)["rocm"]
    assert report.default == "cpu" and not rocm.available
    assert "sees no GPU" in (rocm.detail or "") and "HSA_OVERRIDE_GFX_VERSION" in (
        rocm.detail or ""
    )


def test_hardware_report_without_torch(no_torch) -> None:
    report = hardware_report()
    devices = _by_id(report)
    assert report.default == "cpu" and devices["cpu"].available
    assert "not installed" in (devices["rocm"].detail or "")


def test_hardware_report_survives_a_broken_driver(use_torch) -> None:
    torch = use_torch("rocm")

    def broken(index: int) -> str:
        raise RuntimeError("HIP error: invalid device ordinal")

    torch.cuda.get_device_name = broken
    rocm = _by_id(hardware_report())["rocm"]
    assert not rocm.available and "GPU probe failed" in (rocm.detail or "")


# ------------------------------------------------------------------------ broken ROCm kernels
def test_broken_rocm_kernels_are_switched_off(use_torch) -> None:
    """gfx1151 + AMD's Windows torch 2.14.0+rocm10.1.0: AOTriton SDPA has no code object and
    MIOpen cannot compile its kernels; both must fall back instead of failing every job."""
    torch = use_torch("rocm", broken=("flash_attention", "efficient_attention", "miopen"))
    assert disable_broken_rocm_kernels() == ["flash_attention", "efficient_attention", "miopen"]
    assert torch.backends.cuda.flash is False and torch.backends.cuda.efficient is False
    assert torch.backends.cudnn.enabled is False


def test_working_rocm_kernels_stay_on(use_torch) -> None:
    torch = use_torch("rocm", broken=("flash_attention",))
    assert disable_broken_rocm_kernels() == ["flash_attention"]
    assert torch.backends.cuda.efficient is True and torch.backends.cudnn.enabled is True


def test_cuda_builds_are_not_probed(use_torch) -> None:
    torch = use_torch("cuda", broken=("flash_attention", "miopen"))
    assert disable_broken_rocm_kernels() == []
    assert torch.backends.cuda.flash is True and torch.backends.cudnn.enabled is True


# ---------------------------------------------------------------------------------- cpu threads
def test_cpu_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("ASR_TORCH_THREADS", "6")
    assert cpu_threads() == 6
    monkeypatch.setenv("ASR_TORCH_THREADS", "0")
    assert cpu_threads() == min(16, os.cpu_count() or 4)


def test_funasr_models_get_the_thread_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """funasr's AutoModel resets torch to ``ncpu`` (default 4) threads on build and on every
    generate call; the CPU path ran 1.7-2x slower with 4 of 16 threads."""
    from asr.providers.local import models
    from asr.providers.local.asr_paraformer import ParaformerAsr
    from asr.providers.local.diar_campplus import CamPlusDiarizer
    from asr.providers.local.vad import FsmnVad

    calls: list[dict[str, Any]] = []
    funasr = types.ModuleType("funasr")
    funasr.AutoModel = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    cluster = types.ModuleType("funasr.models.campplus.cluster_backend")
    cluster.ClusterBackend = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", funasr)
    monkeypatch.setitem(sys.modules, "funasr.models.campplus.cluster_backend", cluster)
    for key in ("fsmn-vad", "campplus", "seaco-paraformer"):
        path = models.model_path(tmp_path, key)
        path.mkdir(parents=True)
        (path / models.spec(key).marker).write_bytes(b"x")
    monkeypatch.setenv("ASR_TORCH_THREADS", "12")
    FsmnVad(tmp_path)
    CamPlusDiarizer(tmp_path)
    ParaformerAsr(tmp_path)
    assert [c["ncpu"] for c in calls] == [12, 12, 12]


# --------------------------------------------------------------------------------- load_engines
class _Recording:
    devices: list[tuple[str, str]]

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args: Any, device: str = "cpu", **kwargs: Any) -> object:
        _Recording.devices.append((self.name, device))
        return object()


@pytest.fixture
def recorded_engines(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import asr.providers.local.align as align_mod
    import asr.providers.local.asr_transformers as tr_mod
    import asr.providers.local.asr_whisper as whisper_mod
    import asr.providers.local.diar_campplus as diar_mod
    import asr.providers.local.vad as vad_mod

    _Recording.devices = []
    for mod, name in [
        (tr_mod, "TransformersAsr"),
        (align_mod, "ForcedAligner"),
        (whisper_mod, "WhisperAsr"),
        (diar_mod, "CamPlusDiarizer"),
        (vad_mod, "FsmnVad"),
    ]:
        monkeypatch.setattr(mod, name, _Recording(name))
    threads: list[int] = []
    monkeypatch.setattr(pipeline_mod, "configure_threads", lambda: threads.append(1) or 1)
    monkeypatch.setattr(pipeline_mod, "engine_installed", lambda key: True)
    monkeypatch.setattr(pipeline_mod, "_ENGINE_CACHE", {})
    _Recording.threads = threads  # type: ignore[attr-defined]
    return _Recording.devices


def test_load_engines_runs_rocm_on_torch_cuda(
    use_torch, recorded_engines: list[tuple[str, str]], tmp_path: Path
) -> None:
    use_torch("rocm")
    pipeline_mod.load_engines(tmp_path, "qwen3-asr-1.7b", device="rocm", dtype="auto")
    assert dict(recorded_engines) == {
        "TransformersAsr": "cuda",
        "ForcedAligner": "cuda",
        "FsmnVad": "cuda",
        "CamPlusDiarizer": "cuda",
    }
    assert _Recording.threads == []  # type: ignore[attr-defined]  # GPU run: no CPU thread cap


def test_load_engines_whisper_on_rocm_uses_cpu(
    use_torch, recorded_engines: list[tuple[str, str]], tmp_path: Path
) -> None:
    use_torch("rocm")
    pipeline_mod.load_engines(tmp_path, WHISPER, device="rocm", dtype="auto")
    assert dict(recorded_engines) == {
        "WhisperAsr": "cpu",
        "FsmnVad": "cuda",
        "CamPlusDiarizer": "cuda",
    }


def test_load_engines_rocm_on_cpu_torch_is_a_provider_error(
    use_torch, recorded_engines: list[tuple[str, str]], tmp_path: Path
) -> None:
    use_torch(None)
    with pytest.raises(ProviderError, match="needs a ROCm build of torch"):
        pipeline_mod.load_engines(tmp_path, "qwen3-asr-1.7b", device="rocm", dtype="auto")
    assert recorded_engines == []


def test_whisper_job_on_rocm_warns_about_cpu(use_torch, tmp_path: Path) -> None:
    use_torch("rocm")

    class Asr:
        def transcribe_timed(self, chunks, *, language=None, terms=()):
            return [("Hello there.", [("Hello", 0.1, 0.5), ("there", 0.6, 1.0)]) for _ in chunks]

    engines = pipeline_mod.LocalEngines(
        vad=FakeVad(), asr=Asr(), aligner=None, diarizer=FakeDiarizer()
    )
    provider = pipeline_mod.LocalPipelineProvider(
        cache_dir=tmp_path, asr_model=WHISPER, device="rocm", engines=engines
    )
    audio = AudioSource(path=write_audio(tmp_path, "wav_s16", seconds=2.0))
    t = provider.transcribe(audio, JobOptions(provider="local", language_hints=["en"]), Recorder())
    assert any("no ROCm build" in w for w in t.warnings)


# --------------------------------------------------------------------------------------- server
def test_env_and_options_accept_rocm(env_factory: EnvFactory) -> None:
    env = env_factory(ASR_DEVICE="rocm")
    assert env.device == "rocm"
    assert effective_settings(env, {}).local_device == "rocm"
    assert effective_settings(env_factory(), {"local.device": "rocm"}).local_device == "rocm"
    assert LocalOptions(device="rocm").device == "rocm"


ROCM_REPORT = HardwareReport(
    (
        DeviceStatus("cpu", True, "AMD RYZEN AI MAX+ 395", "32 logical cores"),
        DeviceStatus("cuda", False, None, "torch 2.14.0+rocm10.1.0 is a ROCm build"),
        DeviceStatus("rocm", True, "AMD Radeon(TM) 8060S Graphics", "gfx1151, HIP 10.1"),
    ),
    "rocm",
)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from server import hardware

    calls: list[int] = []

    def fake() -> HardwareReport:
        calls.append(1)
        return ROCM_REPORT

    monkeypatch.setattr(hardware, "hardware_report", fake)
    return calls


async def test_hardware_route(client: httpx.AsyncClient, probe: list[int]) -> None:
    resp = await client.get("/api/system/hardware")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "devices": [
            {
                "id": "cpu",
                "available": True,
                "name": "AMD RYZEN AI MAX+ 395",
                "detail": "32 logical cores",
            },
            {
                "id": "cuda",
                "available": False,
                "name": None,
                "detail": "torch 2.14.0+rocm10.1.0 is a ROCm build",
            },
            {
                "id": "rocm",
                "available": True,
                "name": "AMD Radeon(TM) 8060S Graphics",
                "detail": "gfx1151, HIP 10.1",
            },
        ],
        "default": "rocm",
    }
    await client.get("/api/system/hardware")
    assert probe == [1]  # probed once per app


async def test_hardware_route_needs_the_api_key(
    make_client, api_key_env: EnvSettings, probe: list[int]
) -> None:
    async with make_client(api_key_env) as c:
        assert (await c.get("/api/system/hardware")).status_code == 401
    assert probe == []


async def test_startup_warm_up_only_for_local_inference(
    env_factory: EnvFactory, make_client, probe: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server import hardware

    monkeypatch.setattr(hardware.local_models, "local_stack_installed", lambda: True)
    async with make_client(env_factory()) as c:  # cpu + cloud default: no probe at startup
        assert (await c.get("/api/health")).status_code == 200
    assert probe == []
    async with make_client(env_factory(ASR_DEVICE="rocm")) as c:
        app = c._transport.app  # type: ignore[attr-defined]
        assert (await app.state.hardware.get()) is ROCM_REPORT
    assert probe == [1]


async def test_jobs_check_the_device_against_the_report(
    app: FastAPI,
    client: httpx.AsyncClient,
    env: EnvSettings,
    probe: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.routes import jobs as jobs_routes

    _fake_models(env, ["fsmn-vad", "qwen3-asr-0.6b", "qwen3-forced-aligner", "campplus"])
    install(app, FakeProvider())
    monkeypatch.setattr(jobs_routes.local_models, "local_stack_installed", lambda: True)
    upload = await add_upload(app, env)

    def body(device: str) -> dict[str, Any]:
        local = {"asr_model": "qwen3-asr-0.6b", "device": device}
        return {"upload_id": upload.id, "provider": "local", "options": {"local": local}}

    resp = await client.post("/api/jobs", json=body("cpu"))
    assert resp.status_code == 201, resp.text
    assert probe == []  # cpu needs no probe
    resp = await client.post("/api/jobs", json=body("cuda"))
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "device 'cuda' is not available: torch 2.14.0+rocm10.1.0 is a ROCm build"
    )
    resp = await client.post("/api/jobs", json=body("rocm"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["options"]["local"]["device"] == "rocm"
    assert (await client.post("/api/jobs", json=body("tpu"))).status_code == 422


async def test_settings_accept_rocm(client: httpx.AsyncClient) -> None:
    resp = await client.put("/api/settings", json={"local": {"device": "rocm"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["local"]["device"] == "rocm"


def test_cli_device_rocm(use_torch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from asr.cli import app
    from asr.providers.local import models

    monkeypatch.setenv("ASR_MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(models, "local_stack_installed", lambda: True)
    audio = write_audio(tmp_path, "wav_s16", seconds=1.0)
    args = ["transcribe", str(audio), "--provider", "local", "--asr-model", "qwen3-asr-0.6b"]
    runner = CliRunner()
    use_torch(None)
    result = runner.invoke(app, [*args, "--device", "rocm"])
    assert result.exit_code == 2 and "needs a ROCm build of torch" in result.output
    use_torch("rocm")
    result = runner.invoke(app, [*args, "--device", "rocm"])
    assert result.exit_code == 2 and "asr models download" in result.output  # got past the check
    assert runner.invoke(app, [*args, "--device", "tpu"]).exit_code == 2
