"""Cloud provider (Alibaba DashScope filetrans). Owned by the ``cloud-provider`` agent (M2)."""

from asr.providers.base import ConnectionTestResult
from asr.providers.cloud.dashscope_filetrans import (
    ALLOWED_ENDPOINTS,
    DashScopeFiletransProvider,
    is_allowed_endpoint,
    test_connection,
)

__all__ = [
    "ALLOWED_ENDPOINTS",
    "ConnectionTestResult",
    "DashScopeFiletransProvider",
    "is_allowed_endpoint",
    "test_connection",
]
