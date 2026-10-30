"""SiYuan (思源笔记) kernel API client: create a transcript document and set its attributes.

Findings and links: ``docs/research/siyuan-export.md``. In short: every call is
``POST <endpoint>/api/...`` with a JSON body and ``Authorization: Token <token>``; the answer is
``{"code": 0, "msg": "", "data": ...}`` with a non-zero ``code`` on failure (auth failures are
HTTP 401). ``createDocWithMd`` always creates a new document, even when the human-readable path
(hpath) already exists, so ``push`` picks a free path first (``title (2)``, ...) and creates the
missing parent documents itself (SiYuan 3.8.9 stopped doing that, siyuan#21082).

Security: the token is sent only to the configured endpoint. The caller's ``httpx.Client`` must
not follow redirects (each request also passes ``follow_redirects=False``), a redirect answer is
an error, and no error message contains the token. Responses are capped at
``MAX_RESPONSE_BYTES``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from asr.export.markdown import one_line

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_PATH_TEMPLATE",
    "PATH_FIELDS",
    "Notebook",
    "PushResult",
    "SiYuanClient",
    "SiYuanError",
    "is_block_id",
    "normalize_endpoint",
    "normalize_path",
    "render_path",
    "validate_path_template",
]

DEFAULT_ENDPOINT = "http://127.0.0.1:6806"
DEFAULT_PATH_TEMPLATE = "/会议记录/{date} {title}"
PATH_FIELDS: tuple[str, ...] = ("title", "date", "time", "source", "provider")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 1000
MAX_SEGMENT_CHARS = 200
MAX_DUPLICATES = 99

_BLOCK_ID = re.compile(r"[0-9]{14}-[0-9a-z]{7}")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class SiYuanError(Exception):
    """A SiYuan call failed. The message is safe to show and log (never contains the token)."""


@dataclass(frozen=True, slots=True)
class Notebook:
    id: str
    name: str
    closed: bool


@dataclass(frozen=True, slots=True)
class PushResult:
    doc_id: str
    path: str
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------- validation
def is_block_id(value: str) -> bool:
    """SiYuan block / notebook ids look like ``20210817205410-2kvfpfn``."""
    return _BLOCK_ID.fullmatch(value) is not None


def normalize_endpoint(raw: str) -> str:
    """``" http://127.0.0.1:6806/ " -> "http://127.0.0.1:6806"``; ``ValueError`` unless an
    absolute http(s) URL without credentials, query or fragment."""
    value = raw.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise ValueError("endpoint must be an absolute http(s) URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("endpoint must be an absolute http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("endpoint must not contain credentials")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ValueError("endpoint must not have a query or fragment")
    return value


def _title_segment(value: str) -> str:
    """One document title: a single line, ``/`` (the hpath separator) as a full-width ``／``
    (as SiYuan's API docs recommend), at most ``MAX_SEGMENT_CHARS`` characters."""
    text = " ".join(one_line(value).replace("/", "／").split())
    return text[:MAX_SEGMENT_CHARS].strip()


def _join(segments: list[str]) -> str:
    kept = [s for s in segments if s]
    if not kept:
        raise ValueError("path has no document title")
    path = "/" + "/".join(kept)
    if len(path) > MAX_PATH_CHARS:
        raise ValueError(f"path is longer than {MAX_PATH_CHARS} characters")
    return path


def render_path(template: str, values: Mapping[str, str]) -> str:
    """Fill ``{title}``-style placeholders (``PATH_FIELDS``) per path segment. Values never add
    hierarchy levels; empty segments are dropped."""
    segments = []
    for seg in template.strip().split("/")[1:]:
        filled = _PLACEHOLDER.sub(lambda m: _title_segment(values.get(m.group(1), "")), seg)
        segments.append(_title_segment(filled))
    return _join(segments)


def validate_path_template(template: str) -> str:
    """The stripped template, or ``ValueError`` naming the problem."""
    value = template.strip()
    if not value.startswith("/"):
        raise ValueError("path_template must start with '/'")
    for name in _PLACEHOLDER.findall(value):
        if name not in PATH_FIELDS:
            fields = ", ".join("{" + f + "}" for f in PATH_FIELDS)
            raise ValueError(f"unknown placeholder {{{name}}} in path_template; use {fields}")
    rest = _PLACEHOLDER.sub("", value)
    if "{" in rest or "}" in rest:
        raise ValueError("unbalanced braces in path_template")
    render_path(value, dict.fromkeys(PATH_FIELDS, "x"))
    return value


def normalize_path(raw: str) -> str:
    """A caller-supplied hpath (``/会议记录/周会``): starts with ``/``, ``/`` separates levels,
    each title made single-line; empty levels dropped."""
    value = raw.strip()
    if not value.startswith("/"):
        raise ValueError("path must start with '/'")
    return _join([" ".join(one_line(s).split())[:MAX_SEGMENT_CHARS] for s in value.split("/")])


# ------------------------------------------------------------------------------------- client
class SiYuanClient:
    def __init__(self, endpoint: str, token: str, *, http: httpx.Client) -> None:
        self.endpoint = normalize_endpoint(endpoint)
        self._token = token
        self._http = http

    def _safe(self, text: str) -> str:
        text = one_line(text)[:300]
        return text.replace(self._token, "****") if self._token else text

    def _read(self, resp: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise SiYuanError("SiYuan response too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def call(self, api: str, payload: Mapping[str, Any] | None = None) -> Any:
        """``POST <endpoint><api>``; returns ``data`` or raises ``SiYuanError``."""
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Token {self._token}"
        try:
            with self._http.stream(
                "POST",
                f"{self.endpoint}{api}",
                json=dict(payload or {}),
                headers=headers,
                follow_redirects=False,
            ) as resp:
                status = resp.status_code
                if 300 <= status < 400:
                    raise SiYuanError(
                        f"SiYuan answered {api} with a redirect (HTTP {status}); check the "
                        "endpoint (redirects are not followed, so the token stays on the "
                        "configured host)"
                    )
                body = self._read(resp)
        except SiYuanError:
            raise
        except httpx.TimeoutException:
            raise SiYuanError(f"SiYuan at {self.endpoint} did not answer in time") from None
        except httpx.HTTPError as exc:
            raise SiYuanError(
                f"cannot reach SiYuan at {self.endpoint} ({type(exc).__name__})"
            ) from None
        if status in (401, 403):
            raise SiYuanError(
                f"SiYuan refused the request (HTTP {status}): check the API token (SiYuan: "
                "Settings - About - API token); access from another machine also needs an "
                "access authorization code in SiYuan"
            )
        if status >= 400:
            raise SiYuanError(f"SiYuan {api} failed with HTTP {status}")
        try:
            answer = json.loads(body)
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or "code" not in answer:
            raise SiYuanError(f"{self.endpoint} did not answer like a SiYuan kernel ({api})")
        if answer["code"] != 0:
            msg = self._safe(str(answer.get("msg") or "")) or f"code {answer['code']}"
            raise SiYuanError(f"SiYuan {api} failed: {msg}")
        return answer.get("data")

    # ------------------------------------------------------------------------------- endpoints
    def version(self) -> str:
        data = self.call("/api/system/version")
        return self._safe(str(data)) if data is not None else ""

    def notebooks(self) -> list[Notebook]:
        data = self.call("/api/notebook/lsNotebooks")
        items = data.get("notebooks") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SiYuanError("unexpected answer from SiYuan /api/notebook/lsNotebooks")
        return [
            Notebook(id=str(n["id"]), name=str(n.get("name") or ""), closed=bool(n.get("closed")))
            for n in items
            if isinstance(n, dict) and isinstance(n.get("id"), str) and is_block_id(n["id"])
        ]

    def ids_by_hpath(self, notebook: str, path: str) -> list[str]:
        data = self.call("/api/filetree/getIDsByHPath", {"notebook": notebook, "path": path})
        return [i for i in data if isinstance(i, str)] if isinstance(data, list) else []

    def create_doc(self, notebook: str, path: str, markdown: str, tags: str = "") -> str:
        payload: dict[str, Any] = {"notebook": notebook, "path": path, "markdown": markdown}
        if tags:
            payload["tags"] = tags
        data = self.call("/api/filetree/createDocWithMd", payload)
        if not isinstance(data, str) or not is_block_id(data):
            raise SiYuanError("unexpected answer from SiYuan /api/filetree/createDocWithMd")
        return data

    def set_block_attrs(self, block_id: str, attrs: Mapping[str, str]) -> None:
        self.call("/api/attr/setBlockAttrs", {"id": block_id, "attrs": dict(attrs)})

    # ----------------------------------------------------------------------------------- push
    def free_path(self, notebook: str, path: str) -> str:
        """``path``, or ``path (2)``, ``path (3)``, ... if a document already has that hpath."""
        if not self.ids_by_hpath(notebook, path):
            return path
        for n in range(2, MAX_DUPLICATES + 1):
            candidate = f"{path} ({n})"
            if not self.ids_by_hpath(notebook, candidate):
                return candidate
        raise SiYuanError(f"too many documents named like {path!r} in this notebook")

    def ensure_parents(self, notebook: str, path: str) -> None:
        """Create the missing ancestors of ``path`` as empty documents, top down."""
        levels = path.strip("/").split("/")[:-1]
        for depth in range(1, len(levels) + 1):
            parent = "/" + "/".join(levels[:depth])
            if not self.ids_by_hpath(notebook, parent):
                self.create_doc(notebook, parent, "")

    def push(
        self,
        notebook: str,
        path: str,
        markdown: str,
        *,
        attrs: Mapping[str, str] | None = None,
        tags: str = "",
    ) -> PushResult:
        """Create the document at a free ``path`` (parents first), then set ``attrs``. An
        attribute failure is reported as a warning: the document exists by then, and failing
        would invite a retry that creates a second copy."""
        target = self.free_path(notebook, path)
        self.ensure_parents(notebook, target)
        doc_id = self.create_doc(notebook, target, markdown, tags)
        warnings: list[str] = []
        if attrs:
            try:
                self.set_block_attrs(doc_id, attrs)
            except SiYuanError as exc:
                warnings.append(f"document created, but its attributes were not set: {exc}")
        return PushResult(doc_id=doc_id, path=target, warnings=warnings)
