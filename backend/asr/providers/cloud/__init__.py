"""Cloud provider (Alibaba DashScope filetrans). Owned by the ``cloud-provider`` agent (M2).

Modules: ``dashscope_filetrans`` (provider + connection test), ``client`` (REST calls, retries),
``transfer`` (temporary upload / public URL), ``normalize`` (result JSON → ``Transcript``),
``security`` (endpoint allow-list, result-URL checks, redaction).
"""

from asr.providers.base import ConnectionTestResult
from asr.providers.cloud.dashscope_filetrans import (
    ALLOWED_ENDPOINTS,
    DEFAULT_MODEL,
    DashScopeFiletransProvider,
    build_request_body,
    is_allowed_endpoint,
    test_connection,
)

__all__ = [
    "ALLOWED_ENDPOINTS",
    "DEFAULT_MODEL",
    "ConnectionTestResult",
    "DashScopeFiletransProvider",
    "build_request_body",
    "is_allowed_endpoint",
    "test_connection",
]
