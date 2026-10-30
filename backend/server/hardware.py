"""Hardware report for ``GET /api/system/hardware`` and job validation, probed once per app.

``asr.providers.local.device.hardware_report`` imports torch, which takes seconds, so it runs in a
worker thread at most once per application: the lifespan starts it in the background when local
inference is configured (``ASR_DEVICE`` is a GPU or the default provider is local), otherwise the
first request that needs it does. Nothing is downloaded.
"""

from __future__ import annotations

import asyncio

from asr.providers.local import device
from asr.providers.local import models as local_models
from server.config import EnvSettings


def hardware_report() -> device.HardwareReport:
    """Indirection so tests can substitute the probe (they never import torch, D9)."""
    return device.hardware_report()


class HardwareCache:
    def __init__(self) -> None:
        self._task: asyncio.Future[device.HardwareReport] | None = None

    def start(self) -> asyncio.Future[device.HardwareReport]:
        if self._task is None:
            self._task = asyncio.ensure_future(asyncio.to_thread(hardware_report))
        return self._task

    async def get(self) -> device.HardwareReport:
        return await asyncio.shield(self.start())

    async def status(self, device_id: str) -> device.DeviceStatus:
        report = await self.get()
        return next(d for d in report.devices if d.id == device_id)


def warm_up(cache: HardwareCache, env: EnvSettings) -> None:
    """Probe in the background at startup when the server is set up for local inference."""
    if (env.device != "cpu" or env.provider_default == "local") and (
        local_models.local_stack_installed()
    ):
        cache.start()
