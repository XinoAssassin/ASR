"""Provider registry. Owned by the ``cloud-provider`` agent (M2)."""

from typing import Any

from asr.providers.base import ProviderName, TranscriptionProvider


def get_provider(name: ProviderName, **config: Any) -> TranscriptionProvider:
    """Instantiate a provider by name.

    ``cloud`` -> ``DashScopeFiletransProvider(**config)``; ``local`` raises
    ``ProviderError("not available until M5")``.
    """
    raise NotImplementedError
