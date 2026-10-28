"""Alibaba DashScope ``qwen-audio-3.1-asr-flash-filetrans`` provider (EXECUTION_LOG §3.3).

API facts verified against the official docs on 2026-10-29:

- Filetrans HTTP API (submit / query / parameters / result JSON):
  https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api
  (EN mirror: https://www.alibabacloud.com/help/en/model-studio/fun-asr-recorded-speech-recognition-http-api)
- User guide (12 h / 2 GB limits, diarization ≤ 2 h recommended, mono-only diarization):
  https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide
- Temporary upload (getPolicy → OSS PostObject → ``oss://``):
  https://help.aliyun.com/zh/model-studio/get-temporary-file-url
- Async task management (status values, ``POST /api/v1/tasks/{id}/cancel`` for PENDING tasks):
  https://help.aliyun.com/zh/model-studio/manage-asynchronous-tasks
- Model page (Qianwen platform endpoint ``cn-beijing.maas.qianwenaiapi.com``):
  https://www.qianwenai.com/models/qwen-audio-3.1-asr-flash-filetrans

Request body::

    {"model": "...",
     "input": {"file_urls": [url],                       # exactly one URL; oss:// via REST only
               "context": [{"role": "user",              # optional; ≤ 5 input_text messages,
                            "content": [{"type": "input_text", "text": "..."}]}]},  # ≤ 400 chars
     "parameters": {                                     # must be present, even {}
        "channel_id": [0],                               # default [0]; each channel billed
        "diarization_enabled": true,                     # mono audio only
        "speaker_count": 2..100,                         # only with diarization
        "vocabulary": {"term": 1..5 | 50},               # instant hotwords; ≤ 50 at weight 50
        "language_hints": ["zh", "en"],                  # qwen-audio-3.x: first 4 used
        "keep_dialect": false}}                          # qwen-audio-3.1 only

Headers: ``Authorization: Bearer``, ``Content-Type: application/json``,
``X-DashScope-Async: enable`` and, for ``oss://`` URLs, ``X-DashScope-OssResourceResolve:
enable``. Task status: ``PENDING | RUNNING | SUCCEEDED | FAILED | UNKNOWN``; a task is
``SUCCEEDED`` when any subtask succeeded, so ``results[].subtask_status`` is checked too.
``transcription_url`` is a signed OSS URL valid 24 h. There is no ITN or word-timestamp switch;
``words`` are always present in the result. Audio preparation follows EXECUTION_LOG D8.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from asr.audio import channels as audio_channels
from asr.audio import decode as audio_decode
from asr.audio import transcode as audio_transcode
from asr.audio.types import ProbeInfo, ResolvedChannels
from asr.providers.base import (
    BOOSTED_TERM_WEIGHT,
    AudioSource,
    ConnectionTestResult,
    FileTransferMode,
    JobCancelled,
    JobOptions,
    ProgressCallback,
    ProgressEvent,
    ProviderError,
    Term,
    Transcript,
)
from asr.providers.cloud import client as client_mod
from asr.providers.cloud.client import DEFAULT_TIMEOUT, DashScopeClient, DashScopeHTTPError
from asr.providers.cloud.normalize import to_transcript
from asr.providers.cloud.security import (
    ALLOWED_ENDPOINTS,
    is_allowed_endpoint,
    redact,
)
from asr.providers.cloud.transfer.public_url import validate_public_url
from asr.providers.cloud.transfer.temp_upload import MB, UploadPolicy, fetch_policy, upload_file

__all__ = [
    "ALLOWED_ENDPOINTS",
    "DashScopeFiletransProvider",
    "build_request_body",
    "is_allowed_endpoint",
    "test_connection",
]

log = logging.getLogger("asr.providers.cloud")

DEFAULT_MODEL = "qwen-audio-3.1-asr-flash-filetrans"
TARGET_RATE = 16_000
DIARIZATION_MAX_S = 2 * 3600
MAX_LANGUAGE_HINTS = 4
MAX_VOCABULARY = 2000
MAX_BOOSTED_TERMS = 50
CONTEXT_CHUNK_CHARS = 400
CONTEXT_MAX_MESSAGES = 5
SPEAKER_COUNT_RANGE = (2, 100)
POLICY_REFRESH_MARGIN_S = 60.0

# Overall progress per stage (EXECUTION_LOG §3.4).
P_PREPARING = 0.02
P_TRANSCODING = 0.04
P_UPLOAD_START, P_UPLOAD_END = 0.10, 0.30
P_QUEUED = 0.32
P_RECOGNIZING_START, P_RECOGNIZING_END = 0.35, 0.90
P_FINALIZING = 0.92


# ------------------------------------------------------------------------------- request body
def _vocabulary(terms: list[Term], warnings: list[str]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for t in terms:
        merged[t.term] = max(merged.get(t.term, 0), t.weight)
    boosted = [k for k, w in merged.items() if w == BOOSTED_TERM_WEIGHT]
    if len(boosted) > MAX_BOOSTED_TERMS:
        for k in boosted[MAX_BOOSTED_TERMS:]:
            merged[k] = 5
        warnings.append(
            f"Only {MAX_BOOSTED_TERMS} boosted terms are allowed; "
            f"{len(boosted) - MAX_BOOSTED_TERMS} were sent with weight 5 instead."
        )
    if len(merged) > MAX_VOCABULARY:
        ranked = sorted(merged.items(), key=lambda kv: -kv[1])  # stable: keeps input order
        kept = dict(ranked[:MAX_VOCABULARY])
        warnings.append(
            f"The cloud vocabulary holds at most {MAX_VOCABULARY} terms; "
            f"{len(merged) - MAX_VOCABULARY} lower-weight terms were dropped."
        )
        merged = {k: v for k, v in merged.items() if k in kept}
    return merged


def _context_messages(context: str, warnings: list[str]) -> list[dict[str, Any]]:
    text = context.strip()
    if not text:
        return []
    limit = CONTEXT_CHUNK_CHARS * CONTEXT_MAX_MESSAGES
    if len(text) > limit:
        warnings.append(f"Context was truncated to {limit} characters.")
        text = text[:limit]
    chunks = [text[i : i + CONTEXT_CHUNK_CHARS] for i in range(0, len(text), CONTEXT_CHUNK_CHARS)]
    return [{"role": "user", "content": [{"type": "input_text", "text": c}]} for c in chunks]


def build_request_body(
    *,
    model: str,
    file_url: str,
    options: JobOptions,
    channel_ids: list[int],
    diarize: bool,
    warnings: list[str],
) -> dict[str, Any]:
    """Submit body for one file. Appends user-visible notes to ``warnings``."""
    parameters: dict[str, Any] = {"channel_id": list(channel_ids)}
    if diarize:
        parameters["diarization_enabled"] = True
        n = options.num_speakers
        if n is not None and SPEAKER_COUNT_RANGE[0] <= n <= SPEAKER_COUNT_RANGE[1]:
            parameters["speaker_count"] = n
    vocabulary = _vocabulary(options.terms, warnings)
    if vocabulary:
        parameters["vocabulary"] = vocabulary
    hints = list(dict.fromkeys(h.strip() for h in options.language_hints if h.strip()))
    if len(hints) > MAX_LANGUAGE_HINTS:
        warnings.append(f"Only the first {MAX_LANGUAGE_HINTS} language hints are used.")
        hints = hints[:MAX_LANGUAGE_HINTS]
    if hints:
        parameters["language_hints"] = hints
    if options.cloud.keep_dialect:
        parameters["keep_dialect"] = True

    body_input: dict[str, Any] = {"file_urls": [file_url]}
    if options.context:
        messages = _context_messages(options.context, warnings)
        if messages:
            body_input["context"] = messages
    return {"model": model, "input": body_input, "parameters": parameters}


# --------------------------------------------------------------------------------- progress
class _Reporter:
    """Forwards monotonic progress to the job's callback (which may raise JobCancelled)."""

    def __init__(self, callback: ProgressCallback) -> None:
        self._callback = callback
        self.value = 0.0
        self.external_id: str | None = None

    def __call__(
        self,
        stage: str,
        progress: float,
        message: str | None = None,
        external_id: str | None = None,
    ) -> None:
        self.value = min(max(self.value, progress), 1.0)
        if external_id is not None:
            self.external_id = external_id
        self._callback(
            ProgressEvent(
                stage=stage, progress=self.value, message=message, external_id=external_id
            )
        )


@dataclass
class _Prepared:
    """Audio ready to be referenced in the submit request."""

    url: str | None = None  # public URL (url sources)
    path: Path | None = None  # file to upload (path sources)
    upload_name: str = "audio"
    policy: UploadPolicy | None = None
    policy_at: float = 0.0
    channel_ids: list[int] = field(default_factory=lambda: [0])
    per_channel: bool = False
    duration: float | None = None


# --------------------------------------------------------------------------------- provider
class DashScopeFiletransProvider:
    """Cloud provider: temp upload (or public URL) → async filetrans task → ``Transcript``."""

    name: Literal["cloud"] = "cloud"
    max_retries: int = 5
    retry_backoff: float = 1.0

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str,
        work_dir: Path,
        file_transfer: FileTransferMode = "temp_upload",
        http: httpx.Client | None = None,
        poll_interval: float = 3.0,
        timeout_s: float = 6 * 3600,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ProviderError("DashScope API key is not configured")
        if not is_allowed_endpoint(endpoint):
            raise ProviderError("DashScope endpoint is not in the allow-list")
        if file_transfer not in ("temp_upload", "public_url"):
            raise ProviderError(f"Unsupported file transfer mode: {file_transfer}")
        self._api_key = api_key.strip()
        self.endpoint = endpoint.rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.work_dir = Path(work_dir)
        self.file_transfer: FileTransferMode = file_transfer
        self._http = http
        self.poll_interval = max(float(poll_interval), 0.0)
        self.timeout_s = float(timeout_s)

    def __repr__(self) -> str:  # never show the key
        return f"DashScopeFiletransProvider(endpoint={self.endpoint!r}, model={self.model!r})"

    @contextlib.contextmanager
    def _client(self) -> Iterator[DashScopeClient]:
        if self._http is not None:
            yield self._make_client(self._http)
            return
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False) as http:
            yield self._make_client(http)

    def _make_client(self, http: httpx.Client) -> DashScopeClient:
        return DashScopeClient(
            api_key=self._api_key,
            endpoint=self.endpoint,
            http=http,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
        )

    # ------------------------------------------------------------------------ transcribe
    def transcribe(
        self, source: AudioSource, options: JobOptions, progress: ProgressCallback
    ) -> Transcript:
        report = _Reporter(progress)
        warnings: list[str] = []
        temp_files: list[Path] = []
        started = time.monotonic()
        try:
            with self._client() as client:
                report("preparing", P_PREPARING, "Preparing audio")
                if source.url is not None:
                    prepared = self._prepare_url(source.url, options)
                    file_url, oss = prepared.url, False
                else:
                    assert source.path is not None  # noqa: S101 - AudioSource validator
                    prepared = self._prepare_path(
                        client, source, options, report, temp_files, warnings
                    )
                    file_url, oss = self._upload(client, prepared, report), True
                assert file_url is not None  # noqa: S101

                diarize = not prepared.per_channel and options.num_speakers != 1
                if (
                    diarize
                    and prepared.duration is not None
                    and prepared.duration > DIARIZATION_MAX_S
                ):
                    warnings.append(
                        "Audio is longer than 2 hours; DashScope speaker diarization may fail "
                        "or time out on recordings this long."
                    )
                body = build_request_body(
                    model=self.model,
                    file_url=file_url,
                    options=options,
                    channel_ids=prepared.channel_ids,
                    diarize=diarize,
                    warnings=warnings,
                )
                task_id = self._submit(client, body, oss=oss)
                output = self._wait(client, task_id, report, prepared.duration, started)
                report("finalizing", P_FINALIZING, "Downloading the result")
                results = self._download_results(client, output, warnings)
                return to_transcript(
                    results,
                    per_channel=prepared.per_channel,
                    language=options.language_hints[0] if options.language_hints else None,
                    fallback_duration=prepared.duration,
                    warnings=warnings,
                )
        except (JobCancelled, ProviderError):
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(f"DashScope request failed: {type(exc).__name__}") from None
        except OSError as exc:
            raise ProviderError(
                f"Cannot access the audio or work files: {exc.strerror or type(exc).__name__}"
            ) from None
        finally:
            for tmp in temp_files:
                with contextlib.suppress(OSError):
                    tmp.unlink()

    # ---------------------------------------------------------------------- preparation
    def _prepare_url(self, url: str, options: JobOptions) -> _Prepared:
        """URL sources are used as-is (D3): never fetched, never transcoded."""
        validate_public_url(url)
        option = options.preprocess.channels
        if option == "per_channel":
            return _Prepared(url=url, channel_ids=[0, 1], per_channel=True)
        if option == "right":
            return _Prepared(url=url, channel_ids=[1])
        return _Prepared(url=url, channel_ids=[0])

    def _prepare_path(
        self,
        client: DashScopeClient,
        source: AudioSource,
        options: JobOptions,
        report: _Reporter,
        temp_files: list[Path],
        warnings: list[str],
    ) -> _Prepared:
        path = Path(source.path)  # type: ignore[arg-type]
        stream_index = options.preprocess.stream_index
        probe = source.probe
        if probe is None:
            try:
                probe = audio_decode.probe(path, stream_index)
            except (ValueError, OSError) as exc:
                raise ProviderError(f"Cannot read the audio file: {redact(str(exc))}") from None
        try:
            resolved: ResolvedChannels = audio_channels.resolve_channels(
                probe, options.preprocess.channels
            )
        except ValueError as exc:
            raise ProviderError(str(exc)) from None

        policy = fetch_policy(client, self.model)
        policy_at = time.monotonic()
        size = path.stat().st_size

        multi = probe.channels > 1
        mono_target = resolved.mode in ("mono", "downmix") and multi
        need = mono_target or size > policy.max_bytes or stream_index != 0
        upload_path, upload_name = path, path.name
        if need:
            reason = (
                "Downmixing to 16 kHz mono FLAC"
                if mono_target
                else "Transcoding to 16 kHz FLAC to fit the upload limit"
            )
            report("preparing", P_TRANSCODING, reason)
            upload_path = self._transcode(
                path, probe, stream_index, mono_target, policy, temp_files
            )
            upload_name = f"{path.stem}{upload_path.suffix}"
            if time.monotonic() - policy_at > self._policy_ttl(policy):
                policy, policy_at = fetch_policy(client, self.model), time.monotonic()

        if resolved.mode == "per_channel":
            channel_ids, per_channel = list(resolved.channel_ids), True
        elif resolved.mode == "select":
            channel_ids, per_channel = list(resolved.channel_ids), False
        else:
            channel_ids, per_channel = [0], False
        return _Prepared(
            path=upload_path,
            upload_name=upload_name,
            policy=policy,
            policy_at=policy_at,
            channel_ids=channel_ids,
            per_channel=per_channel,
            duration=probe.duration,
        )

    @staticmethod
    def _policy_ttl(policy: UploadPolicy) -> float:
        ttl = float(policy.expire_in_seconds or 300)
        return max(ttl - POLICY_REFRESH_MARGIN_S, 0.0)

    def _transcode(
        self,
        path: Path,
        probe: ProbeInfo,
        stream_index: int,
        mono: bool,
        policy: UploadPolicy,
        temp_files: list[Path],
    ) -> Path:
        """16 kHz FLAC (mono when downmixing); MP3 if the FLAC is still above the cap."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out_channels = 1 if mono else probe.channels
        stem = f"cloud-{uuid.uuid4().hex[:12]}"
        flac = self.work_dir / f"{stem}.flac"
        temp_files.append(flac)
        try:
            audio_transcode.transcode_audio(
                path,
                flac,
                fmt="flac",
                sample_rate=TARGET_RATE,
                channels=1 if mono else None,
                stream_index=stream_index,
            )
        except ValueError as exc:
            raise ProviderError(f"Cannot transcode the audio file: {redact(str(exc))}") from None
        size = flac.stat().st_size
        if size <= policy.max_bytes:
            return flac
        if out_channels <= 2:
            mp3 = self.work_dir / f"{stem}.mp3"
            temp_files.append(mp3)
            try:
                audio_transcode.transcode_audio(
                    path,
                    mp3,
                    fmt="mp3",
                    sample_rate=TARGET_RATE,
                    channels=1 if mono else None,
                    stream_index=stream_index,
                    bitrate=64_000 if out_channels == 1 else 96_000,
                )
            except ValueError as exc:
                raise ProviderError(
                    f"Cannot transcode the audio file: {redact(str(exc))}"
                ) from None
            size = mp3.stat().st_size
            if size <= policy.max_bytes:
                return mp3
        raise ProviderError(
            f"Audio is still {size / MB:.0f} MB after transcoding, above the "
            f"{policy.max_file_size_mb} MB DashScope upload limit. Split the recording or use "
            "a public URL."
        )

    def _upload(self, client: DashScopeClient, prepared: _Prepared, report: _Reporter) -> str:
        assert prepared.path is not None and prepared.policy is not None  # noqa: S101
        policy = prepared.policy
        if time.monotonic() - prepared.policy_at > self._policy_ttl(policy):
            policy = fetch_policy(client, self.model)
        report("uploading", P_UPLOAD_START, "Uploading to DashScope temporary storage")
        last = [P_UPLOAD_START]

        def on_progress(sent: int, total: int) -> None:
            value = P_UPLOAD_START + (P_UPLOAD_END - P_UPLOAD_START) * (sent / max(total, 1))
            if value - last[0] >= 0.005 or sent >= total:
                last[0] = value
                report("uploading", value)

        url = upload_file(
            client,
            policy,
            prepared.path,
            model=self.model,
            filename=prepared.upload_name,
            on_progress=on_progress,
        )
        report("uploading", P_UPLOAD_END, "Upload complete")
        return url

    # --------------------------------------------------------------------- remote task
    def _submit(self, client: DashScopeClient, body: dict[str, Any], *, oss: bool) -> str:
        response = client.submit(body, oss_resolve=oss)
        output = response.get("output") if isinstance(response.get("output"), dict) else {}
        task_id = output.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("DashScope did not return a task id")
        log.info("DashScope task %s submitted (model %s)", task_id, self.model)
        return task_id

    def _wait(
        self,
        client: DashScopeClient,
        task_id: str,
        report: _Reporter,
        duration: float | None,
        started: float,
    ) -> dict[str, Any]:
        """Poll until SUCCEEDED; cancel the remote task on JobCancelled / timeout."""
        deadline = started + self.timeout_s
        estimate = max(30.0, (duration or 600.0) / 15.0)
        running_since: float | None = None
        try:
            report("cloud_queued", P_QUEUED, "Queued at DashScope", external_id=task_id)
            while True:
                task = client.get_task(task_id)
                output = task.get("output") if isinstance(task.get("output"), dict) else {}
                status = str(output.get("task_status") or "").upper()
                if status == "SUCCEEDED":
                    return output
                if status == "FAILED":
                    raise ProviderError(self._failure_message(client, output))
                if status in ("UNKNOWN", "CANCELED", "CANCELLED"):
                    raise ProviderError(f"DashScope task ended with status {status}")
                if status == "RUNNING":
                    now = time.monotonic()
                    running_since = running_since or now
                    frac = 1.0 - math.exp(-(now - running_since) / estimate)
                    value = P_RECOGNIZING_START + (P_RECOGNIZING_END - P_RECOGNIZING_START) * frac
                    report("recognizing", value, "Recognizing at DashScope")
                else:  # PENDING or a status we do not know yet
                    report("cloud_queued", P_QUEUED, "Queued at DashScope")
                if time.monotonic() > deadline:
                    client.cancel_task(task_id)
                    raise ProviderError(
                        f"DashScope task did not finish within {self.timeout_s / 3600:.1f} h"
                    )
                client_mod.sleep(self.poll_interval)
        except (JobCancelled, KeyboardInterrupt):
            client.cancel_task(task_id)
            raise

    @staticmethod
    def _failure_message(client: DashScopeClient, output: dict[str, Any]) -> str:
        code, message = output.get("code"), output.get("message")
        for result in output.get("results") or []:
            if isinstance(result, dict) and result.get("subtask_status") == "FAILED":
                code = code or result.get("code")
                message = message or result.get("message")
        detail = ": ".join(str(x) for x in (code, message) if x) or "no details"
        return client.safe(f"DashScope transcription failed ({detail})")

    def _download_results(
        self, client: DashScopeClient, output: dict[str, Any], warnings: list[str]
    ) -> list[dict[str, Any]]:
        results = [r for r in (output.get("results") or []) if isinstance(r, dict)]
        ok = [r for r in results if str(r.get("subtask_status") or "").upper() == "SUCCEEDED"]
        failed = [r for r in results if r not in ok]
        if not ok:
            raise ProviderError(self._failure_message(client, {"results": results}))
        if failed:
            warnings.append(f"{len(failed)} of {len(results)} DashScope subtasks failed.")
        files: list[dict[str, Any]] = []
        for result in ok:
            url = result.get("transcription_url")
            if not isinstance(url, str) or not url:
                raise ProviderError("DashScope result has no transcription URL")
            files.append(client.download_json(url))
        return files


# ------------------------------------------------------------------------- connection test
def test_connection(
    *, api_key: str, endpoint: str, model: str, http: httpx.Client | None = None
) -> ConnectionTestResult:
    """Verify the DashScope key against ``endpoint`` by requesting an upload policy.

    Never raises; no network call when the key is empty or the endpoint is not allowed.
    """
    if not api_key or not api_key.strip():
        return ConnectionTestResult(ok=False, message="No DashScope API key is configured")
    if not is_allowed_endpoint(endpoint):
        return ConnectionTestResult(ok=False, message="DashScope endpoint is not in the allow-list")
    key = api_key.strip()

    def run(h: httpx.Client) -> ConnectionTestResult:
        client = DashScopeClient(api_key=key, endpoint=endpoint, http=h, max_retries=1)
        policy = fetch_policy(client, model or DEFAULT_MODEL)
        return ConnectionTestResult(
            ok=True,
            message=f"Connected; upload limit {policy.max_file_size_mb} MB",
            max_file_size_mb=policy.max_file_size_mb,
        )

    try:
        if http is not None:
            return run(http)
        with httpx.Client(timeout=httpx.Timeout(15.0), follow_redirects=False) as h:
            return run(h)
    except DashScopeHTTPError as exc:
        return ConnectionTestResult(ok=False, message=redact(str(exc), (key,)))
    except ProviderError as exc:
        return ConnectionTestResult(ok=False, message=redact(str(exc), (key,)))
    except httpx.HTTPError as exc:
        return ConnectionTestResult(
            ok=False, message=f"Could not reach DashScope: {type(exc).__name__}"
        )
    except Exception as exc:  # pragma: no cover - defensive: the settings route must not fail
        log.error("DashScope connection test raised %s", type(exc).__name__)
        return ConnectionTestResult(ok=False, message="Connection test failed unexpectedly")


# Not a pytest test, even when imported into a test module.
test_connection.__test__ = False  # type: ignore[attr-defined]
