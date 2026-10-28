"""Live smoke test of the DashScope cloud provider. COSTS MONEY — run deliberately.

Usage (from ``backend/``)::

    uv run python scripts/live_cloud_test.py PATH [--num-speakers N] [--save-fixtures DIR]

Credentials: ``DASHSCOPE_API_KEY`` / ``DASHSCOPE_ENDPOINT`` / ``ASR_CLOUD_MODEL`` from the
environment, falling back to the backend settings loader (which also reads ``./.env``). The
key is never printed. Prints speaker count, segment count, duration and the first 10 txt lines;
exits 2 without a key and 1 on a provider error.

``--save-fixtures DIR`` writes every DashScope JSON response (policy, submit, each poll, result)
for future respx tests, sanitised: task / request ids are kept; ``policy``, ``signature``,
``oss_access_key_id`` and every URL query string (``Signature``, ``OSSAccessKeyId``,
``Expires``) are replaced by placeholders. Result files contain the transcript text of the
recording — review them before committing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from asr.providers.base import AudioSource, JobOptions, ProgressEvent, ProviderError
from asr.providers.cloud import DEFAULT_MODEL, DashScopeFiletransProvider
from asr.writers import write

SECRET_KEYS = {
    "policy": "POLICY_PLACEHOLDER",
    "signature": "SIGNATURE_PLACEHOLDER",
    "oss_access_key_id": "ACCESS_KEY_ID_PLACEHOLDER",
}
_URL_WITH_QUERY = re.compile(r"^((?:https?|oss)://[^?#]+)[?#].*$", re.IGNORECASE)
_SIGNED_QUERY = "Expires=0&OSSAccessKeyId=ACCESS_KEY_ID_PLACEHOLDER&Signature=SIGNATURE_PLACEHOLDER"


def sanitise(value: Any, api_key: str) -> Any:
    """Replace secrets and signed URL queries; keep ids and the documented structure."""
    if isinstance(value, dict):
        return {
            k: (SECRET_KEYS[k] if k in SECRET_KEYS and isinstance(v, str) else sanitise(v, api_key))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitise(v, api_key) for v in value]
    if isinstance(value, str):
        if api_key and api_key in value:
            value = value.replace(api_key, "sk-REDACTED")
        match = _URL_WITH_QUERY.match(value)
        if match:
            return f"{match.group(1)}?{_SIGNED_QUERY}"
    return value


def _credentials() -> tuple[str, str, str]:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    endpoint = os.environ.get("DASHSCOPE_ENDPOINT", "").strip()
    model = os.environ.get("ASR_CLOUD_MODEL", "").strip()
    if not key or not endpoint or not model:
        try:
            from server.config import get_env

            env = get_env()
            key = key or env.dashscope_api_key.get_secret_value().strip()
            endpoint = endpoint or env.dashscope_endpoint
            model = model or env.cloud_model
        except Exception as exc:  # settings are optional here
            print(f"(settings loader unavailable: {type(exc).__name__})", file=sys.stderr)
    return key, endpoint or "https://dashscope.aliyuncs.com", model or DEFAULT_MODEL


class FixtureRecorder:
    """httpx response hook collecting sanitised JSON responses."""

    def __init__(self, directory: Path, api_key: str) -> None:
        self.directory = directory
        self.api_key = api_key
        self.polls = 0
        self.results = 0

    def __call__(self, response: httpx.Response) -> None:
        response.read()
        try:
            body = response.json()
        except ValueError:
            return  # OSS upload (empty / XML)
        path = urlsplit(str(response.request.url)).path
        if path.endswith("/api/v1/uploads"):
            name = "live_get_policy.json"
        elif path.endswith("/services/audio/asr/transcription"):
            name = "live_submit.json"
        elif "/api/v1/tasks/" in path and path.endswith("/cancel"):
            name = "live_cancel.json"
        elif "/api/v1/tasks/" in path:
            self.polls += 1
            status = str((body.get("output") or {}).get("task_status", "unknown")).lower()
            name = f"live_task_{self.polls:03d}_{status}.json"
        else:
            self.results += 1
            name = f"live_result_{self.results}.json"
        clean = sanitise(body, self.api_key)
        clean["_status_code"] = response.status_code
        (self.directory / name).write_text(
            json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--num-speakers", type=int, default=None)
    parser.add_argument("--channels", default="auto")
    parser.add_argument("--save-fixtures", type=Path, default=None, metavar="DIR")
    args = parser.parse_args()

    key, endpoint, model = _credentials()
    if not key:
        print("DASHSCOPE_API_KEY is not set (env or .env); aborting.", file=sys.stderr)
        return 2
    if not args.path.is_file():
        print(f"not a file: {args.path}", file=sys.stderr)
        return 2

    hooks: dict[str, list[Any]] = {"response": []}
    if args.save_fixtures:
        args.save_fixtures.mkdir(parents=True, exist_ok=True)
        hooks["response"].append(FixtureRecorder(args.save_fixtures, key))

    def progress(event: ProgressEvent) -> None:
        extra = f" task={event.external_id}" if event.external_id else ""
        print(f"[{event.progress:5.1%}] {event.stage}{extra}", file=sys.stderr)

    options = JobOptions(num_speakers=args.num_speakers)
    options.preprocess.channels = args.channels
    print(f"endpoint={endpoint} model={model} file={args.path.name}", file=sys.stderr)
    with (
        tempfile.TemporaryDirectory(prefix="asr-live-") as work,
        httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0), event_hooks=hooks) as http,
    ):
        try:
            provider = DashScopeFiletransProvider(
                api_key=key, endpoint=endpoint, model=model, work_dir=Path(work), http=http
            )
            transcript = provider.transcribe(AudioSource(path=args.path), options, progress)
        except ProviderError as exc:
            print(f"provider error: {exc}", file=sys.stderr)
            return 1

    print(f"speakers: {len(transcript.speakers)}")
    print(f"segments: {len(transcript.segments)}")
    print(f"duration: {transcript.duration}")
    for warning in transcript.warnings:
        print(f"warning: {warning}")
    print("--- first 10 lines ---")
    for line in write(transcript, "txt").splitlines()[:10]:
        print(line)
    if args.save_fixtures:
        print(f"sanitised responses written to {args.save_fixtures}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
