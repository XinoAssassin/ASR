"""Uploads (PLAN §5.1). Owned by ``backend-core`` (M1).

Bodies are streamed to disk: the multipart route parses the request stream itself (python-
multipart) so the 2 GiB cap is enforced while receiving, and chunked parts are written from
``request.stream()``. Every stored file is probed; undecodable uploads are deleted and answered
with 415.
"""

import asyncio
import contextlib
import time
import uuid
import weakref
from datetime import UTC, datetime
from pathlib import Path as FsPath
from typing import Annotated, Any, BinaryIO

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from asr.audio.decode import AudioDecodeError, probe
from asr.audio.formats import is_accepted
from server import storage
from server.deps import DbDep, EnvDep
from server.schemas import (
    MAX_UPLOAD_SIZE,
    UploadInitIn,
    UploadInitOut,
    UploadOut,
    UploadPartOut,
    UploadRegisterIn,
)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_OCTET_STREAM_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}

# Same shape FastAPI generated for ``File()`` + ``Form()`` parameters; the route parses the
# stream itself so the size cap applies while receiving.
_MULTIPART_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "title": "Body_upload_file_api_uploads_post",
                    "type": "object",
                    "required": ["file", "target_dir"],
                    "properties": {
                        "file": {
                            "title": "File",
                            "type": "string",
                            "format": "binary",
                            "contentMediaType": "application/octet-stream",
                            "description": "Audio/video file.",
                        },
                        "target_dir": {"title": "Target Dir", "type": "string", "maxLength": 4096},
                    },
                }
            }
        },
    }
}

_MAX_FIELD_BYTES = 4096 * 4  # target_dir is <= 4096 characters
_MAX_FIELDS = 16
_MULTIPART_OVERHEAD = 1024 * 1024  # headers / boundaries allowed on top of the file cap


def _http(exc: storage.StorageError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


async def _close_and_discard(fh: BinaryIO, path: FsPath) -> None:
    def _discard() -> None:
        with contextlib.suppress(OSError):
            fh.close()
        path.unlink(missing_ok=True)

    await run_in_threadpool(_discard)


async def _register(
    db: DbDep,
    path: FsPath,
    original_name: str,
    *,
    delete_on_failure: bool,
    registered: bool = False,
) -> UploadOut:
    """Probe a stored file and insert the upload row."""
    try:
        info = await run_in_threadpool(probe, path)
    except AudioDecodeError as exc:
        if delete_on_failure:
            await run_in_threadpool(path.unlink, True)
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"not a decodable audio file: {exc}"
        ) from exc
    except BaseException:
        if delete_on_failure:
            await run_in_threadpool(path.unlink, True)
        raise
    upload = UploadOut(
        id=uuid.uuid4().hex,
        path=str(path),
        original_name=original_name,
        size=info.size,
        created_at=datetime.now(UTC),
        registered=registered,
        probe=info,
    )
    await db.insert_upload(upload)
    return upload


# ---------------------------------------------------------------------------------- multipart
class _MultipartReceiver:
    """Callbacks for python-multipart: one file part (streamed to a temp file) plus fields."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.filename: str | None = None
        self.file_size = 0
        self.pending: list[bytes] = []  # file bytes not yet written (written in a threadpool)
        self._headers: dict[bytes, bytes] = {}
        self._header_name = b""
        self._header_value = b""
        self._name: str | None = None
        self._is_file = False
        self._field = bytearray()
        self.error: storage.StorageError | None = None

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value_cb,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _fail(self, exc: storage.StorageError) -> None:
        self.error = exc
        raise exc

    def _part_begin(self) -> None:
        self._headers = {}
        self._name = None
        self._is_file = False
        self._field = bytearray()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _header_value_cb(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._header_name.lower()] = self._header_value
        self._header_name = b""
        self._header_value = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        raw_name = options.get(b"name")
        if raw_name is None:
            self._fail(storage.InvalidUploadError("multipart part without a name"))
        self._name = raw_name.decode("utf-8", "replace")
        if b"filename" in options:
            if self._name != "file" or self.filename is not None:
                self._fail(storage.InvalidUploadError("exactly one 'file' part is expected"))
            self.filename = options[b"filename"].decode("utf-8", "replace")
            if not is_accepted(self.filename):
                suffix = FsPath(self.filename).suffix or "(none)"
                self._fail(storage.UnsupportedFormatError(f"unsupported file type: {suffix}"))
            self._is_file = True
        elif len(self.fields) >= _MAX_FIELDS:
            self._fail(storage.InvalidUploadError("too many form fields"))

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._is_file:
            self.file_size += end - start
            if self.file_size > MAX_UPLOAD_SIZE:
                self._fail(storage.TooLargeError("file exceeds the 2 GiB upload limit"))
            self.pending.append(data[start:end])
        else:
            self._field += data[start:end]
            if len(self._field) > _MAX_FIELD_BYTES:
                self._fail(storage.InvalidUploadError("form field too long"))

    def _part_end(self) -> None:
        if not self._is_file and self._name is not None:
            self.fields[self._name] = self._field.decode("utf-8", "replace")


@router.post(
    "",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=_MULTIPART_BODY,
)
async def upload_file(request: Request, env: EnvDep, db: DbDep) -> UploadOut:
    """Single-request multipart upload (curl; the UI uses chunked upload, D11).

    Form fields: ``file`` (the audio/video file) and ``target_dir`` (absolute, or relative to the
    first upload root).
    """
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "expected multipart/form-data with a boundary"
        )
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_UPLOAD_SIZE + _MULTIPART_OVERHEAD:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "file exceeds the 2 GiB upload limit"
        )

    receiver = _MultipartReceiver()
    parser = MultipartParser(boundary, receiver.callbacks())
    tmp, fh = await run_in_threadpool(storage.new_temp_file, env.data_dir)
    try:
        try:
            async for chunk in request.stream():
                parser.write(chunk)
                if receiver.pending:
                    data = b"".join(receiver.pending)
                    receiver.pending.clear()
                    await run_in_threadpool(fh.write, data)
            parser.finalize()
        except storage.StorageError as exc:
            raise _http(exc) from exc
        except FormParserError as exc:
            if isinstance(receiver.error, storage.StorageError):
                raise _http(receiver.error) from exc
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid multipart body"
            ) from exc
        await run_in_threadpool(fh.close)
        if receiver.filename is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "missing 'file' part")
        target_dir = receiver.fields.get("target_dir")
        if target_dir is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "missing 'target_dir'")
        if len(target_dir) > 4096:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "target_dir too long")
        try:
            target = await run_in_threadpool(
                storage.resolve_target_dir, target_dir, env.upload_roots
            )
            stored = await run_in_threadpool(storage.move_into, tmp, target, receiver.filename)
        except storage.StorageError as exc:
            raise _http(exc) from exc
    except BaseException:
        await _close_and_discard(fh, tmp)
        raise
    return await _register(
        db, stored, storage.display_name(receiver.filename), delete_on_failure=True
    )


# ------------------------------------------------------------------------------------ chunked
@router.post("/init", response_model=UploadInitOut, status_code=status.HTTP_201_CREATED)
async def init_upload(body: UploadInitIn, env: EnvDep) -> UploadInitOut:
    """Start a chunked upload: ``total_parts`` parts of ``part_size`` bytes (last one shorter)."""
    try:
        session = await run_in_threadpool(
            storage.create_session,
            env.data_dir,
            env.upload_roots,
            filename=body.filename,
            size=body.size,
            target_dir=body.target_dir,
        )
    except storage.StorageError as exc:
        raise _http(exc) from exc
    return UploadInitOut(
        session_id=session.session_id,
        part_size=session.part_size,
        total_parts=session.total_parts,
    )


@router.put(
    "/{session_id}/parts/{n}",
    response_model=UploadPartOut,
    openapi_extra=_OCTET_STREAM_BODY,
)
async def upload_part(
    session_id: str, n: Annotated[int, Path(ge=0)], request: Request, env: EnvDep
) -> UploadPartOut:
    """Raw part body (``application/octet-stream``); read via ``request.stream()``.

    Every part must be exactly ``part_size`` bytes except the last. Re-sending a part replaces
    it (idempotent).
    """
    try:
        session = await run_in_threadpool(storage.load_session, env.data_dir, session_id)
        expected = session.expected_part_size(n)
    except storage.StorageError as exc:
        raise _http(exc) from exc
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) != expected:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"part {n} must be {expected} bytes, got Content-Length {length}",
        )

    tmp, fh = await run_in_threadpool(storage.open_part_writer, env.data_dir, session, n)
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > expected:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"part {n} must be {expected} bytes, received more",
                )
            await run_in_threadpool(fh.write, chunk)
        await run_in_threadpool(fh.close)
        if received != expected:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"part {n} must be {expected} bytes, received {received}",
            )
        await run_in_threadpool(storage.commit_part, env.data_dir, session, n, tmp)
    except BaseException:
        await _close_and_discard(fh, tmp)
        raise
    return UploadPartOut(n=n, size=received)


COMPLETED_TTL_S = 3600.0


class _Completions:
    """Makes ``complete`` idempotent: a client whose first ``complete`` succeeded but whose
    response was lost (timeout, desktop retry queue) gets the same upload back for an hour.
    Concurrent duplicates wait for the first call instead of racing it. In memory: a restart
    forgets it, and a repeat then answers 404 as before."""

    def __init__(self) -> None:
        self._done: dict[str, tuple[str, float]] = {}  # session id -> (upload id, monotonic)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def get(self, session_id: str) -> str | None:
        now = time.monotonic()
        for sid, (_, at) in list(self._done.items()):
            if now - at > COMPLETED_TTL_S:
                del self._done[sid]
        entry = self._done.get(session_id)
        return None if entry is None else entry[0]

    def add(self, session_id: str, upload_id: str) -> None:
        self._done[session_id] = (upload_id, time.monotonic())


_completions = _Completions()


@router.post(
    "/{session_id}/complete",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": UploadOut, "description": "Already completed (repeated call)."}},
)
async def complete_upload(session_id: str, response: Response, env: EnvDep, db: DbDep) -> UploadOut:
    """Assemble the parts in order into ``target_dir`` and probe the result.

    Idempotent for an hour: repeating ``complete`` for a session that already completed returns
    the same upload with 200 (a concurrent repeat waits for the first call)."""
    async with _completions.lock(session_id):
        done = _completions.get(session_id)
        if done is not None:
            upload = await db.get_upload(done)
            if upload is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload session")
            response.status_code = status.HTTP_200_OK
            return upload
        try:
            stored, original_name = await run_in_threadpool(
                storage.complete_session, env.data_dir, env.upload_roots, session_id
            )
        except storage.StorageError as exc:
            raise _http(exc) from exc
        upload = await _register(db, stored, original_name, delete_on_failure=True)
        _completions.add(session_id, upload.id)
        return upload


# ----------------------------------------------------------------------------------- register
@router.post("/register", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def register_upload(body: UploadRegisterIn, env: EnvDep, db: DbDep) -> UploadOut:
    """Register a file that already exists inside an upload root (no transfer)."""
    try:
        path = await run_in_threadpool(storage.resolve_register_path, body.path, env.upload_roots)
    except storage.StorageError as exc:
        raise _http(exc) from exc
    return await _register(db, path, path.name, delete_on_failure=False, registered=True)


@router.get("/{upload_id}", response_model=UploadOut)
async def get_upload(upload_id: str, db: DbDep) -> UploadOut:
    upload = await db.get_upload(upload_id)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload")
    return upload
