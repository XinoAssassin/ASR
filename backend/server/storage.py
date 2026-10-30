"""Upload-root validation and file persistence. Owned by ``backend-core`` (M1).

AGENTS.md rule 2: every user-supplied path must resolve (realpath) inside one of
``EnvSettings.upload_roots``; reject ``..``, absolute paths outside roots and symlink escapes.
Containment is checked with ``os.path.commonpath`` on realpaths, never with a string prefix
(``/data/uploads2`` is not inside ``/data/uploads``).

Chunked-upload sessions live on disk under ``ASR_DATA_DIR/upload-sessions/{session_id}/``
(EXECUTION_LOG D18): a JSON manifest plus one file per received part. Everything here is
blocking filesystem work; routes call it via ``run_in_threadpool``.

Errors derive from ``StorageError`` and carry the HTTP status from EXECUTION_LOG §3.5. A full
disk (``ENOSPC`` / ``EDQUOT``) becomes ``InsufficientStorageError`` (507), never a bare 500.

Disk budget of a chunked upload of ``size`` bytes (what ``complete_session`` really does): the
parts (1x) and ``assembled.tmp`` (1x) coexist under ``ASR_DATA_DIR``, so that volume needs 2x.
``move_into`` then renames the result into the target directory (no extra space) or, when the
target is on another volume, copies it there (1x on that volume, 3x across both). ``init`` checks
that budget plus what other active sessions still need; ``complete`` re-checks its own share.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

from asr.audio.formats import is_accepted
from server.schemas import MAX_UPLOAD_SIZE

PART_SIZE = 8 * 1024 * 1024  # 8 MiB (EXECUTION_LOG D11)
MAX_OPEN_SESSIONS = 16  # concurrently *active* chunked uploads; stale ones are cleaned after 24 h
ACTIVE_SESSION_S = 30 * 60  # a session with no part written for this long no longer counts
FREE_SPACE_MARGIN = 256 * 1024 * 1024  # keep this much disk free after an upload
SESSION_TTL_S = 24 * 3600
SESSIONS_DIRNAME = "upload-sessions"
TMP_DIRNAME = "tmp"
MANIFEST = "session.json"
COMPLETE_LOCK = "complete.lock"
ASSEMBLED = "assembled.tmp"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")  # secrets.token_urlsafe(24)
MAX_FILENAME_BYTES = 200  # leaves room for " (NNNN)" below the usual 255-byte limit
MAX_COLLISIONS = 10_000
_COPY_CHUNK = 1024 * 1024

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{dev}{i}" for dev in ("COM", "LPT") for i in (*"123456789", "¹", "²", "³")}
)
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SEPARATORS = re.compile(r"[\\/]")


# ---------------------------------------------------------------------------------------- errors
class StorageError(Exception):
    """Base class; ``status_code`` is the HTTP status the API maps it to."""

    status_code = 422


class InvalidPathError(StorageError):
    """Malformed path (NUL byte, not a directory / regular file)."""

    status_code = 422


class PathNotAllowedError(StorageError):
    """The path resolves outside every upload root (HTTP 403)."""

    status_code = 403


class UnsupportedFormatError(StorageError):
    """File extension not in ``ACCEPTED_EXTENSIONS`` (HTTP 415)."""

    status_code = 415


class TooLargeError(StorageError):
    """Upload exceeds ``MAX_UPLOAD_SIZE`` (HTTP 413)."""

    status_code = 413


class NotFoundError(StorageError):
    """Unknown session / missing file (HTTP 404)."""

    status_code = 404


class InvalidUploadError(StorageError):
    """Bad part number or size, missing parts, size mismatch (HTTP 422)."""

    status_code = 422


class ConflictError(StorageError):
    """The session is already being completed (HTTP 409)."""

    status_code = 409


class TooManySessionsError(StorageError):
    """Too many chunked uploads in progress (HTTP 429)."""

    status_code = 429


class InsufficientStorageError(StorageError):
    """Not enough free disk space for the declared upload (HTTP 507)."""

    status_code = 507


DISK_FULL_DETAIL = "not enough free disk space on the server for this upload"
_DISK_FULL_ERRNOS = frozenset(
    code for code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if code is not None
)
_DISK_FULL_WINERRORS = frozenset({39, 112})  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL


def is_disk_full(exc: BaseException) -> bool:
    """``exc`` reports a full disk or an exhausted quota."""
    return isinstance(exc, OSError) and (
        exc.errno in _DISK_FULL_ERRNOS or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS
    )


@contextlib.contextmanager
def disk_full_as_507() -> Iterator[None]:
    """Turn a full-disk ``OSError`` raised in the block into ``InsufficientStorageError``.
    The block is responsible for removing its partial files (it sees the ``OSError`` first)."""
    try:
        yield
    except OSError as exc:
        if is_disk_full(exc):
            raise InsufficientStorageError(DISK_FULL_DETAIL) from None
        raise


# --------------------------------------------------------------------------------- path checks
def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


def _is_within(path: str, root: str, mod: ModuleType = os.path) -> bool:
    """``path`` equals or lies under ``root``; both already realpath'd and normcase'd.

    ``mod`` is the path flavour (``os.path``; tests pass ``ntpath`` to check Windows path shapes
    on any OS). A string prefix is never enough, and paths that cannot share a root (another
    drive, UNC vs drive letter, a ``\\\\?\\`` prefix vs none, relative vs absolute) are outside.
    """
    path, root = _anchored(path, mod), _anchored(root, mod)
    try:
        return mod.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows, or mixed absolute / relative
        return False


def _anchored(path: str, mod: ModuleType) -> str:
    """A bare UNC share (``\\\\nas\\media``) is that share's root (``\\\\nas\\media\\``);
    ``ntpath`` would otherwise call it relative and refuse every path under it."""
    drive, rest = mod.splitdrive(path)
    return drive + mod.sep if drive and not rest else path


def _candidate(path: str, root: str, mod: ModuleType = os.path) -> str:
    """``path`` as given if absolute, else joined onto the first root. On Windows a
    drive-relative ``C:foo`` keeps its own drive (``ntpath.join``), so it is checked as a path on
    ``C:`` and fails containment instead of landing under the root."""
    return path if mod.isabs(path) else mod.join(root, path)


def containing_root(path: str | os.PathLike[str], roots: Sequence[Path]) -> Path | None:
    """The root whose realpath contains ``path``'s realpath, or ``None``."""
    real = _norm(path)
    for root in roots:
        if _is_within(real, _norm(root)):
            return Path(os.path.realpath(root))
    return None


def resolve_stored_file(path: str | os.PathLike[str], roots: Sequence[Path]) -> Path:
    """Re-validate a stored upload path right before it is read.

    Files inside a root can be replaced by a symlink after upload / register, so the realpath must
    still lie inside a root. Returns the realpath, so the caller opens the checked file rather than
    following the link again.
    """
    real = Path(os.path.realpath(path))
    if containing_root(real, roots) is None:
        raise PathNotAllowedError("the stored audio file resolves outside the upload roots")
    if not real.is_file():
        raise NotFoundError("audio file no longer exists")
    return real


def _check_raw_path(path: str) -> None:
    if "\x00" in path:
        raise InvalidPathError("path contains a NUL byte")
    if any(part == ".." for part in _SEPARATORS.split(path)):
        raise PathNotAllowedError("path must not contain '..'")


def resolve_in_roots(path: str, roots: Sequence[Path]) -> Path:
    """Resolve ``path`` (absolute, or relative to ``roots[0]``) and require it inside a root.

    Returns the realpath. Symlinks are followed before the containment check, so a link that
    points outside every root is rejected. Nothing is created.
    """
    _check_raw_path(path)
    if not roots:
        raise PathNotAllowedError("no upload roots configured")
    real = Path(os.path.realpath(_candidate(path, str(roots[0]))))
    if containing_root(real, roots) is None:
        raise PathNotAllowedError("path is outside the allowed upload roots")
    return real


def resolve_target_dir(target_dir: str, roots: Sequence[Path]) -> Path:
    """Validate an upload target directory, create it if missing, and return its realpath."""
    real = resolve_in_roots(target_dir, roots)
    if real.exists() and not real.is_dir():
        raise InvalidPathError("target_dir is not a directory")
    try:
        real.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvalidPathError(f"cannot create target_dir: {exc.strerror}") from exc
    # Re-check after creation: a component may have been swapped for a symlink meanwhile.
    again = Path(os.path.realpath(real))
    if containing_root(again, roots) is None:
        raise PathNotAllowedError("path is outside the allowed upload roots")
    if not again.is_dir():
        raise InvalidPathError("target_dir is not a directory")
    return again


def resolve_register_path(path: str, roots: Sequence[Path]) -> Path:
    """Validate an existing server file for ``POST /api/uploads/register``; returns its realpath."""
    real = resolve_in_roots(path, roots)
    if not real.exists():
        raise NotFoundError("file not found")
    if not real.is_file():
        raise InvalidPathError("path is not a regular file")
    if not is_accepted(real.name):
        raise UnsupportedFormatError(f"unsupported file type: {real.suffix or '(none)'}")
    return real


# ------------------------------------------------------------------------------------ filenames
def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and ext and len(ext) <= 16 and ext.isalnum():
        return stem, f".{ext}"
    return name, ""


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def display_name(name: str) -> str:
    """Client filename for display (``UploadOut.original_name``): basename, no control chars."""
    base = _SEPARATORS.split(name)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    return base.strip()[:255] or "upload"


def safe_filename(name: str) -> str:
    """Strip path separators and unsafe characters from a client-supplied filename.

    Keeps the last path component (``/`` and ``\\`` both count), drops control / format
    characters, replaces characters Windows forbids, strips leading dots and spaces (no hidden
    files) and trailing dots and spaces, prefixes Windows reserved device names (``CON``,
    ``COM1`` …) with ``_``, and caps the UTF-8 length while keeping the extension.
    """
    base = _SEPARATORS.split(unicodedata.normalize("NFC", name))[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    base = _UNSAFE_CHARS.sub("_", base)
    base = re.sub(r"\s+", " ", base).strip(" ")
    stem, ext = _split_ext(base)
    stem = stem.strip(" .")
    if not stem:
        stem = "upload"
    if stem.split(".")[0].strip().upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    stem = _truncate_utf8(stem, MAX_FILENAME_BYTES - len(ext.encode("utf-8"))).rstrip(" .")
    return f"{stem or 'upload'}{ext}"


def create_unique_file(directory: Path, filename: str) -> tuple[Path, BinaryIO]:
    """Create ``directory/filename`` exclusively (``O_EXCL``), adding `` (1)``, `` (2)`` … on
    collisions. Returns the path and the open binary file (caller closes it)."""
    stem, ext = _split_ext(filename)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for i in range(MAX_COLLISIONS):
        candidate = directory / (filename if i == 0 else f"{stem} ({i}){ext}")
        try:
            fd = os.open(candidate, flags, 0o644)
        except FileExistsError:
            continue
        return candidate, os.fdopen(fd, "wb")
    raise ConflictError("too many files with the same name in target_dir")


def move_into(src: Path, directory: Path, filename: str) -> Path:
    """Move a finished temp file into ``directory`` under a unique, sanitised ``filename``."""
    dst, placeholder = create_unique_file(directory, safe_filename(filename))
    try:
        placeholder_st = os.fstat(placeholder.fileno())
        try:
            placeholder.close()
            os.replace(src, dst)  # we own ``dst``; replacing it is race-free
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            _copy_into_placeholder(src, dst, placeholder_st)
            src.unlink()
    except BaseException:
        with contextlib.suppress(OSError):
            dst.unlink()
        raise
    return dst


def _copy_into_placeholder(src: Path, dst: Path, placeholder: os.stat_result) -> None:
    """Cross-device fallback of ``move_into``: copy into the file we created, never into
    whatever ``dst`` names now. ``O_NOFOLLOW`` refuses a symlink swapped in at ``dst``, and the
    inode check refuses any other replacement (e.g. a hard link to a file outside the roots)."""
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dst, flags)
    except OSError as exc:
        raise PathNotAllowedError("upload destination changed while storing the file") from exc
    with os.fdopen(fd, "wb") as fout:
        st = os.fstat(fout.fileno())
        if (st.st_dev, st.st_ino) != (placeholder.st_dev, placeholder.st_ino):
            raise PathNotAllowedError("upload destination changed while storing the file")
        fout.truncate(0)
        with open(src, "rb") as fin:
            shutil.copyfileobj(fin, fout, _COPY_CHUNK)


def new_temp_file(data_dir: Path) -> tuple[Path, BinaryIO]:
    """Temp file under ``ASR_DATA_DIR/tmp`` for a streaming upload."""
    tmp_dir = data_dir / TMP_DIRNAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix="upload-", suffix=".part")
    return Path(name), os.fdopen(fd, "wb")


# --------------------------------------------------------------------------- chunked sessions
@dataclass(frozen=True, slots=True)
class UploadSession:
    session_id: str
    filename: str  # sanitised name used on disk
    original_name: str  # client name for display
    size: int
    target_dir: str  # realpath, re-validated on complete
    part_size: int
    total_parts: int
    created_at: float  # epoch seconds

    def expected_part_size(self, n: int) -> int:
        if n < 0 or n >= self.total_parts:
            raise InvalidUploadError(f"part number {n} out of range (0..{self.total_parts - 1})")
        if n < self.total_parts - 1:
            return self.part_size
        return self.size - self.part_size * (self.total_parts - 1)


def sessions_dir(data_dir: Path) -> Path:
    return data_dir / SESSIONS_DIRNAME


def _session_dir(data_dir: Path, session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise NotFoundError("unknown upload session")
    return sessions_dir(data_dir) / session_id


def part_path(data_dir: Path, session_id: str, n: int) -> Path:
    return _session_dir(data_dir, session_id) / f"part-{n:06d}"


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _active_session_dirs(data_dir: Path) -> list[Path]:
    """Sessions that received a part (or were created) recently; abandoned uploads (closed tab,
    cancelled) stop counting after ``ACTIVE_SESSION_S`` and cannot block new uploads."""
    sessions = sessions_dir(data_dir)
    if not sessions.is_dir():
        return []
    cutoff = time.time() - ACTIVE_SESSION_S
    active: list[Path] = []
    for p in sessions.iterdir():
        with contextlib.suppress(OSError):
            if p.is_dir() and p.stat().st_mtime >= cutoff:  # part renames bump the dir mtime
                active.append(p)
    return active


def _active_sessions(data_dir: Path) -> int:
    return len(_active_session_dirs(data_dir))


# ------------------------------------------------------------------------------- disk budget
def _device(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _bytes_on_disk(directory: Path) -> int:
    """Bytes a session already occupies (parts, part temp files, a partial ``assembled.tmp``)."""
    total = 0
    with contextlib.suppress(OSError):
        for entry in os.scandir(directory):
            if entry.name in (MANIFEST, COMPLETE_LOCK):
                continue
            with contextlib.suppress(OSError):
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def _complete_needs(
    data_dir: Path, target: Path, size: int, *, on_disk: int
) -> list[tuple[Path, int]]:
    """Bytes a chunked upload still needs per volume until ``complete`` has finished: parts and
    ``assembled.tmp`` (2 x ``size``) under ``data_dir`` minus what is already written, plus a
    copy on the target volume when it differs (``move_into`` cannot rename across devices)."""
    needs = [(data_dir, max(0, 2 * size - on_disk))]
    if _device(target) != _device(data_dir):
        needs.append((target, size))
    return needs


def _reserved_by_sessions(data_dir: Path) -> list[tuple[Path, int]]:
    """What the other active sessions still need (their declared sizes), so 16 parallel uploads
    cannot all pass the free-space check against the same free bytes."""
    needs: list[tuple[Path, int]] = []
    for directory in _active_session_dirs(data_dir):
        try:
            with open(directory / MANIFEST, encoding="utf-8") as f:
                manifest = json.load(f)
            size, target = int(manifest["size"]), Path(manifest["target_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        needs += _complete_needs(data_dir, target, size, on_disk=_bytes_on_disk(directory))
    return needs


def _require_free_space(needs: Sequence[tuple[Path, int]]) -> None:
    """Every volume in ``needs`` must keep ``FREE_SPACE_MARGIN`` free after the listed bytes."""
    per_device: dict[int, tuple[Path, int]] = {}
    for path, amount in needs:
        dev = _device(path)
        if dev is None:
            continue
        where, total = per_device.get(dev, (path, 0))
        per_device[dev] = (where, total + amount)
    for where, total in per_device.values():
        if shutil.disk_usage(where).free < total + FREE_SPACE_MARGIN:
            raise InsufficientStorageError(DISK_FULL_DETAIL)


def check_space_for_upload(data_dir: Path, size: int) -> None:
    """Single-request upload of ``size`` bytes: the temp file under ``data_dir`` must fit next
    to what active chunked sessions still need. (The target volume is checked before the move,
    ``check_space_for_move``, once ``target_dir`` has been parsed.)"""
    _require_free_space([(data_dir, size), *_reserved_by_sessions(data_dir)])


def check_space_for_move(src: Path, target: Path, size: int) -> None:
    """``move_into`` copies across volumes: the target volume then needs ``size`` more."""
    if _device(target) != _device(src.parent):
        _require_free_space([(target, size)])


# --------------------------------------------------------------------------------- sessions
def create_session(
    data_dir: Path, roots: Sequence[Path], *, filename: str, size: int, target_dir: str
) -> UploadSession:
    """Validate the request and create a session directory with its manifest."""
    if size > MAX_UPLOAD_SIZE:
        raise TooLargeError(f"file exceeds the {MAX_UPLOAD_SIZE // 1024**3} GiB upload limit")
    if size <= 0:
        raise InvalidUploadError("size must be positive")
    if not is_accepted(filename):
        raise UnsupportedFormatError(f"unsupported file type: {Path(filename).suffix or '(none)'}")
    target = resolve_target_dir(target_dir, roots)
    cleanup_stale_sessions(data_dir)
    if _active_sessions(data_dir) >= MAX_OPEN_SESSIONS:
        raise TooManySessionsError(
            f"too many uploads in progress (max {MAX_OPEN_SESSIONS}); finish or wait for them"
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    _require_free_space(
        [*_complete_needs(data_dir, target, size, on_disk=0), *_reserved_by_sessions(data_dir)]
    )
    session = UploadSession(
        session_id=secrets.token_urlsafe(24),
        filename=safe_filename(filename),
        original_name=display_name(filename),
        size=size,
        target_dir=str(target),
        part_size=PART_SIZE,
        total_parts=max(1, -(-size // PART_SIZE)),
        created_at=time.time(),
    )
    directory = _session_dir(data_dir, session.session_id)
    try:
        with disk_full_as_507():
            directory.mkdir(parents=True, exist_ok=False)
            _write_json_atomic(directory / MANIFEST, asdict(session))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return session


def load_session(data_dir: Path, session_id: str) -> UploadSession:
    """Read a session manifest; unknown, malformed or expired sessions are ``NotFoundError``."""
    directory = _session_dir(data_dir, session_id)
    try:
        with open(directory / MANIFEST, encoding="utf-8") as f:
            session = UploadSession(**json.load(f))
    except (OSError, ValueError, TypeError) as exc:
        raise NotFoundError("unknown upload session") from exc
    if session.session_id != session_id:
        raise NotFoundError("unknown upload session")
    if time.time() - session.created_at > SESSION_TTL_S:
        shutil.rmtree(directory, ignore_errors=True)
        raise NotFoundError("upload session expired")
    return session


def open_part_writer(data_dir: Path, session: UploadSession, n: int) -> tuple[Path, BinaryIO]:
    """Temp file for part ``n``; ``commit_part`` renames it into place (idempotent re-PUTs)."""
    session.expected_part_size(n)
    directory = _session_dir(data_dir, session.session_id)
    with disk_full_as_507():
        fd, name = tempfile.mkstemp(dir=directory, prefix=f".part-{n:06d}.", suffix=".tmp")
    return Path(name), os.fdopen(fd, "wb")


def commit_part(data_dir: Path, session: UploadSession, n: int, tmp: Path) -> None:
    os.replace(tmp, part_path(data_dir, session.session_id, n))


def missing_parts(data_dir: Path, session: UploadSession) -> list[int]:
    """Part numbers that are absent or have the wrong size."""
    missing: list[int] = []
    for n in range(session.total_parts):
        try:
            size = part_path(data_dir, session.session_id, n).stat().st_size
        except OSError:
            missing.append(n)
            continue
        if size != session.expected_part_size(n):
            missing.append(n)
    return missing


def _format_parts(parts: list[int]) -> str:
    shown = ", ".join(str(n) for n in parts[:20])
    return shown + (f" … ({len(parts)} total)" if len(parts) > 20 else "")


# ----------------------------------------------------------------------------- complete lock
_LOCK_BUSY_ERRNOS = frozenset(
    code
    for code in (
        errno.EAGAIN,
        errno.EACCES,
        *(getattr(errno, n, None) for n in ("EWOULDBLOCK", "EDEADLOCK")),
    )
    if code is not None
)
_ERROR_LOCK_VIOLATION = 33


def try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock on an open file; ``False`` if another holder has it.

    The OS drops the lock when the holder closes the file or dies (crash, OOM kill, SIGKILL),
    so a lock file left on disk by a dead process never blocks anybody. ``flock`` / ``msvcrt``
    locks belong to the open file, so a second open in the same process conflicts too.
    """
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        # Anything but "held by someone else" means locking is unsupported here (e.g. some
        # network file systems): the per-session asyncio lock in the route still serialises
        # completes, and D46 guarantees a single process.
        busy = exc.errno in _LOCK_BUSY_ERRNOS
        return not (busy or getattr(exc, "winerror", None) == _ERROR_LOCK_VIOLATION)
    return True


@contextlib.contextmanager
def _complete_lock(directory: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(directory / COMPLETE_LOCK, flags, 0o644)
    except FileNotFoundError as exc:  # session removed meanwhile
        raise NotFoundError("unknown upload session") from exc
    try:
        if not try_lock(fd):
            raise ConflictError("upload session is already being completed; retry in a moment")
        yield
    finally:
        os.close(fd)  # releases the lock (also done by the OS if this process dies)


def complete_session(data_dir: Path, roots: Sequence[Path], session_id: str) -> tuple[Path, str]:
    """Assemble all parts in order into the target directory and remove the session.

    Returns ``(stored_path, original_name)``. A full disk raises ``InsufficientStorageError``
    and leaves the parts in place (the client may free space and retry); the partial
    ``assembled.tmp`` and any partial copy in the target directory are removed.
    """
    session = load_session(data_dir, session_id)
    directory = _session_dir(data_dir, session_id)
    with _complete_lock(directory):
        missing = missing_parts(data_dir, session)
        if missing:
            raise InvalidUploadError(f"missing or incomplete parts: {_format_parts(missing)}")
        target = resolve_target_dir(session.target_dir, roots)
        assembled = directory / ASSEMBLED
        assembled.unlink(missing_ok=True)  # left by an attempt that died mid-assembly
        _require_free_space(_complete_needs(data_dir, target, session.size, on_disk=session.size))
        try:
            with disk_full_as_507():
                written = 0
                with open(assembled, "wb") as out:
                    for n in range(session.total_parts):
                        with open(part_path(data_dir, session_id, n), "rb") as part:
                            while chunk := part.read(_COPY_CHUNK):
                                out.write(chunk)
                                written += len(chunk)
                if written != session.size:
                    raise InvalidUploadError(
                        f"assembled size {written} != declared size {session.size}"
                    )
                stored = move_into(assembled, target, session.filename)
        finally:
            assembled.unlink(missing_ok=True)  # already moved away on success
    # The lock file is closed (Windows cannot delete an open file) before the session goes.
    shutil.rmtree(directory, ignore_errors=True)
    return stored, session.original_name


def cleanup_stale_sessions(data_dir: Path, *, now: float | None = None) -> int:
    """Remove sessions older than ``SESSION_TTL_S``; returns how many were removed."""
    now = time.time() if now is None else now
    root = sessions_dir(data_dir)
    if not root.is_dir():
        _cleanup_stale_temp(data_dir, now)
        return 0
    removed = 0
    for entry in root.iterdir():
        if not SESSION_ID_RE.fullmatch(entry.name) or not entry.is_dir():
            continue
        try:
            with open(entry / MANIFEST, encoding="utf-8") as f:
                created = float(json.load(f)["created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            try:
                created = entry.stat().st_mtime
            except OSError:
                continue
        if now - created > SESSION_TTL_S:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    _cleanup_stale_temp(data_dir, now)
    return removed


def _cleanup_stale_temp(data_dir: Path, now: float) -> None:
    """Drop multipart temp files left behind by a crash (same TTL as sessions)."""
    tmp_dir = data_dir / TMP_DIRNAME
    if not tmp_dir.is_dir():
        return
    for entry in tmp_dir.iterdir():
        try:
            if entry.is_file() and now - entry.stat().st_mtime > SESSION_TTL_S:
                entry.unlink()
        except OSError:
            continue
