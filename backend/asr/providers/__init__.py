"""Provider registry. Owned by the ``cloud-provider`` agent (M2)."""

from typing import Any

from asr.providers.base import ProviderError, ProviderName, TranscriptionProvider

LOCAL_UNAVAILABLE = "Local provider is not available until M5"


def get_provider(name: ProviderName, **config: Any) -> TranscriptionProvider:
    """Instantiate a provider by name.

    ``cloud`` -> ``DashScopeFiletransProvider(**config)`` (keyword arguments per EXECUTION_LOG
    §3.3); ``local`` raises ``ProviderError`` until M5.
    """
    if name == "cloud":
        from asr.providers.cloud import DashScopeFiletransProvider

        try:
            return DashScopeFiletransProvider(**config)
        except TypeError:
            raise ProviderError("Invalid cloud provider configuration") from None
    if name == "local":
        raise ProviderError(LOCAL_UNAVAILABLE)
    raise ProviderError(f"Unknown provider: {name!r}")


__all__ = ["LOCAL_UNAVAILABLE", "get_provider"]
