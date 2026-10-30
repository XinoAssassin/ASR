"""Thin synchronous REST client for the DashScope endpoints used by the filetrans provider.

Endpoints (verified 2026-10-29, see ``dashscope_filetrans`` module docstring for sources):

- ``GET  {endpoint}/api/v1/uploads?action=getPolicy&model=…``      upload policy (``data``)
- ``POST {endpoint}/api/v1/services/audio/asr/transcription``       submit (``X-DashScope-Async``)
- ``GET  {endpoint}/api/v1/tasks/{task_id}``                         task status / results
- ``POST {endpoint}/api/v1/tasks/{task_id}/cancel``                  cancel (PENDING tasks only)

All calls send ``Authorization: Bearer <key>`` to the allow-listed endpoint only. Result files
are fetched with ``download_json`` *without* the header. Transient failures (429, 5xx,
timeouts, connection errors) are retried with exponential backoff; idempotency-sensitive
calls (submit) only retry when the request was certainly not processed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from asr.providers.base import ProviderError
from asr.providers.cloud.security import (
    install_log_redaction,
    is_aliyun_https_url,
    is_allowed_endpoint,
    normalize_endpoint,
    redact,
    redact_url,
)

log = logging.getLogger("asr.providers.cloud")
install_log_redaction()

SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
UPLOADS_PATH = "/api/v1/uploads"
TASKS_PATH = "/api/v1/tasks"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0

AUTH_MESSAGE = (
    "DashScope rejected the API key (HTTP {status}). Check the key and that it belongs to the "
    "configured endpoint's platform and region."
)

# Indirection so tests can skip real sleeping.
sleep: Callable[[float], None] = time.sleep


class DashScopeHTTPError(ProviderError):
    """A non-success DashScope response; ``message`` is already redacted."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class DashScopeTransientError(DashScopeHTTPError):
    """Retries ran out on a transient failure (429, 5xx, timeout, connection error): the
    service may well answer again later. ``status`` is ``None`` for transport errors."""


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = response.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    output = body.get("output") if isinstance(body.get("output"), dict) else {}
    code = body.get("code") or output.get("code")
    message = body.get("message") or output.get("message")
    return (str(code) if code else None, str(message) if message else None)


class DashScopeClient:
    """REST calls against one allow-listed endpoint with one API key."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        http: httpx.Client,
        max_retries: int = 5,
        retry_backoff: float = 1.0,
    ) -> None:
        if not is_allowed_endpoint(endpoint):
            raise ProviderError("DashScope endpoint is not in the allow-list")
        self._key = api_key
        self.endpoint = normalize_endpoint(endpoint)
        self.http = http
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    # ---------------------------------------------------------------------------- helpers
    def safe(self, text: object) -> str:
        """Redact the key and signed URLs from ``text``."""
        return redact(str(text), (self._key,))

    def _auth(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._key}"}
        if extra:
            headers.update(extra)
        return headers

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER_S)
                except ValueError:
                    pass
        return min(2.0**attempt, 30.0) * self.retry_backoff

    def raise_for_response(
        self, response: httpx.Response, what: str, *, auth_errors: bool = True
    ) -> None:
        """Raise a redacted ``DashScopeHTTPError`` for non-2xx responses.

        ``auth_errors``: map 401 / 403 to the "rejected the API key" message. Off for requests
        that carry no key (a signed result URL that expired is not a key problem).
        """
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403) and auth_errors:
            code, _ = _error_fields(response)
            raise DashScopeHTTPError(AUTH_MESSAGE.format(status=status), status=status, code=code)
        code, message = _error_fields(response)
        detail = ": ".join(x for x in (code, message) if x) or response.reason_phrase or "error"
        raise DashScopeHTTPError(
            self.safe(f"DashScope {what} failed (HTTP {status}): {detail}"),
            status=status,
            code=code,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        what: str,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
        retry_read_errors: bool = True,
        auth_errors: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send with retries on transient errors; return the final (2xx) response.

        Raises ``DashScopeTransientError`` when the retries run out on a transient failure.
        """
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = self.http.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                failure: str = type(exc).__name__
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if not retry_read_errors:
                    raise ProviderError(
                        f"DashScope {what} failed: {type(exc).__name__} (not retried)"
                    ) from None
                failure = type(exc).__name__
            else:
                if response.status_code not in retry_statuses:
                    self.raise_for_response(response, what, auth_errors=auth_errors)
                    return response
                failure = f"HTTP {response.status_code}"
            if attempt >= self.max_retries:
                if response is not None:
                    try:
                        self.raise_for_response(response, what, auth_errors=auth_errors)
                    except DashScopeHTTPError as exc:
                        raise DashScopeTransientError(
                            str(exc), status=exc.status, code=exc.code
                        ) from None
                raise DashScopeTransientError(f"DashScope {what} failed after retries: {failure}")
            delay = self._delay(attempt, response)
            log.warning(
                "DashScope %s %s: %s; retry %d/%d in %.1fs",
                what,
                redact_url(url),
                failure,
                attempt + 1,
                self.max_retries,
                delay,
            )
            attempt += 1
            sleep(delay)

    def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise ProviderError(f"DashScope {what} returned invalid JSON") from None
        if not isinstance(body, dict):
            raise ProviderError(f"DashScope {what} returned an unexpected payload")
        return body

    # ------------------------------------------------------------------------- endpoints
    def get_policy(self, model: str) -> dict[str, Any]:
        """``data`` of the temporary-upload policy for ``model``."""
        response = self.request(
            "GET",
            f"{self.endpoint}{UPLOADS_PATH}",
            what="upload policy request",
            params={"action": "getPolicy", "model": model},
            headers=self._auth({"Content-Type": "application/json"}),
        )
        data = self._json(response, "upload policy request").get("data")
        if not isinstance(data, dict):
            raise ProviderError("DashScope upload policy response has no data")
        return data

    def submit(self, body: dict[str, Any], *, oss_resolve: bool) -> dict[str, Any]:
        """Submit an async transcription task; returns the response JSON."""
        headers = {"Content-Type": "application/json", "X-DashScope-Async": "enable"}
        if oss_resolve:
            headers["X-DashScope-OssResourceResolve"] = "enable"
        response = self.request(
            "POST",
            f"{self.endpoint}{SUBMIT_PATH}",
            what="task submission",
            # 429 means "not accepted"; 5xx / read timeouts might have created a task already.
            retry_statuses=frozenset({429}),
            retry_read_errors=False,
            json=body,
            headers=self._auth(headers),
        )
        return self._json(response, "task submission")

    def get_task(self, task_id: str) -> dict[str, Any]:
        response = self.request(
            "GET", f"{self.endpoint}{TASKS_PATH}/{task_id}", what="task query", headers=self._auth()
        )
        return self._json(response, "task query")

    def cancel_task(self, task_id: str) -> bool:
        """Best effort; DashScope only cancels PENDING tasks. Never raises."""
        try:
            response = self.http.post(
                f"{self.endpoint}{TASKS_PATH}/{task_id}/cancel",
                headers=self._auth(),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            log.warning("DashScope cancel of task %s failed: %s", task_id, type(exc).__name__)
            return False
        ok = 200 <= response.status_code < 300
        log.info("DashScope cancel of task %s: HTTP %d", task_id, response.status_code)
        return ok

    def download_json(self, url: str) -> dict[str, Any]:
        """Fetch a result file. No Authorization header; https ``*.aliyuncs.com`` only."""
        if not is_aliyun_https_url(url):
            raise ProviderError(
                f"Refusing to download a result from an unexpected host: {redact_url(url)}"
            )
        # No key was sent, so a 401 / 403 is an expired or forbidden signed URL, not the key.
        response = self.request("GET", url, what="result download", auth_errors=False)
        return self._json(response, "result download")
