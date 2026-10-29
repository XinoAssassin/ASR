"""Provider registry."""

from typing import Any

from asr.providers.base import ProviderError, ProviderName, TranscriptionProvider


def get_provider(name: ProviderName, **config: Any) -> TranscriptionProvider:
    """Instantiate a provider by name.

    ``cloud`` -> ``DashScopeFiletransProvider(**config)`` (keyword arguments per EXECUTION_LOG
    §3.3); ``local`` -> ``LocalPipelineProvider(**config)`` (``cache_dir``, ``asr_model``,
    ``device``, ``dtype``).
    """
    if name == "cloud":
        from asr.providers.cloud import DashScopeFiletransProvider

        try:
            return DashScopeFiletransProvider(**config)
        except TypeError:
            raise ProviderError("Invalid cloud provider configuration") from None
    if name == "local":
        from asr.providers.local.pipeline import LocalPipelineProvider

        try:
            return LocalPipelineProvider(**config)
        except TypeError:
            raise ProviderError("Invalid local provider configuration") from None
    raise ProviderError(f"Unknown provider: {name!r}")


__all__ = ["get_provider"]
