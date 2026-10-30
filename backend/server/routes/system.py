"""``GET /api/system/hardware``: which inference devices this server can use."""

from dataclasses import asdict

from fastapi import APIRouter, Request

from server.hardware import HardwareCache
from server.schemas import HardwareOut

router = APIRouter(prefix="/api/system", tags=["system"])


def hardware_cache(request: Request) -> HardwareCache:
    cache: HardwareCache = request.app.state.hardware
    return cache


@router.get("/hardware", response_model=HardwareOut)
async def hardware(request: Request) -> HardwareOut:
    """cpu is always available; cuda needs a CUDA build of torch with a visible NVIDIA GPU, rocm
    a ROCm (HIP) build with a visible AMD GPU. Probed once per server process; no downloads."""
    return HardwareOut.model_validate(asdict(await hardware_cache(request).get()))
