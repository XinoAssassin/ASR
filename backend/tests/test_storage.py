"""Upload-root validation, filename sanitising and chunked sessions (server.storage, rule 2)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from server import storage
from server.schemas import MAX_UPLOAD_SIZE
from server.storage import (
    InvalidPathError,
    InvalidUploadError,
    NotFoundError,
    PathNotAllowedError,
    TooLargeError,
    UnsupportedFormatError,
    create_unique_file,
    resolve_in_roots,
    resolve_register_path,
    resolve_target_dir,
    safe_filename,
)
from tests.fs_helpers import symlink_or_skip


@pytest.fixture
def roots(tmp_path: Path) -> list[Path]:
    a = tmp_path / "uploads"
    b = tmp_path / "media"
    a.mkdir()
    b.mkdir()
    return [a.resolve(), b.resolve()]


# --------------------------------------------------------------------------------- target dirs
def test_relative_target_is_under_first_root(roots: list[Path]) -> None:
    got = resolve_target_dir("2026/meetings", roots)
    assert got == roots[0] / "2026" / "meetings"
    assert got.is_dir()


def test_empty_target_is_first_root(roots: list[Path]) -> None:
    assert resolve_target_dir("", roots) == roots[0]


def test_absolute_target_inside_second_root(roots: list[Path]) -> None:
    got = resolve_target_dir(str(roots[1] / "x"), roots)
    assert got == roots[1] / "x"


@pytest.mark.parametrize(
    "target",
    ["..", "../escape", "a/../../escape", "a/..", "..\\escape", "a\\..\\..\\b", "./.."],
)
def test_dotdot_rejected(roots: list[Path], target: str) -> None:
    with pytest.raises(PathNotAllowedError):
        resolve_target_dir(target, roots)


def test_absolute_outside_roots_rejected(roots: list[Path], tmp_path: Path) -> None:
    for target in (str(tmp_path / "elsewhere"), "/etc", "/"):
        with pytest.raises(PathNotAllowedError):
            resolve_target_dir(target, roots)
    assert not (tmp_path / "elsewhere").exists()


def test_sibling_prefix_root_rejected(roots: list[Path]) -> None:
    sibling = Path(str(roots[0]) + "2")  # /tmp/.../uploads2 is not inside /tmp/.../uploads
    sibling.mkdir()
    with pytest.raises(PathNotAllowedError):
        resolve_target_dir(str(sibling), roots)
    with pytest.raises(PathNotAllowedError):
        resolve_target_dir(str(sibling / "sub"), roots)


def test_symlink_dir_escape_rejected(roots: list[Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(roots[0] / "link", outside, target_is_directory=True)
    with pytest.raises(PathNotAllowedError):
        resolve_target_dir("link", roots)
    with pytest.raises(PathNotAllowedError):
        resolve_target_dir("link/new-subdir", roots)
    assert not (outside / "new-subdir").exists()


def test_symlink_inside_roots_allowed(roots: list[Path]) -> None:
    (roots[1] / "real").mkdir()
    symlink_or_skip(roots[0] / "to-media", roots[1] / "real", target_is_directory=True)
    assert resolve_target_dir("to-media", roots) == roots[1] / "real"


def test_nul_byte_rejected(roots: list[Path]) -> None:
    with pytest.raises(InvalidPathError):
        resolve_target_dir("a\x00b", roots)


def test_target_that_is_a_file_rejected(roots: list[Path]) -> None:
    (roots[0] / "file.m4a").write_bytes(b"x")
    with pytest.raises(InvalidPathError):
        resolve_target_dir("file.m4a", roots)


def test_no_roots_configured() -> None:
    with pytest.raises(PathNotAllowedError):
        resolve_in_roots("x", [])


def test_status_codes() -> None:
    assert PathNotAllowedError.status_code == 403
    assert TooLargeError.status_code == 413
    assert UnsupportedFormatError.status_code == 415
    assert InvalidUploadError.status_code == 422
    assert NotFoundError.status_code == 404


# ------------------------------------------------------------------------------------ register
def test_register_inside_root(roots: list[Path]) -> None:
    f = roots[1] / "a.m4a"
    f.write_bytes(b"x")
    assert resolve_register_path(str(f), roots) == f
    assert resolve_register_path("../media/a.m4a".replace("../media", str(roots[1])), roots) == f


def test_register_relative_to_first_root(roots: list[Path]) -> None:
    (roots[0] / "sub").mkdir()
    (roots[0] / "sub" / "b.wav").write_bytes(b"x")
    assert resolve_register_path("sub/b.wav", roots) == roots[0] / "sub" / "b.wav"


def test_register_outside_root(roots: list[Path], tmp_path: Path) -> None:
    f = tmp_path / "secret.m4a"
    f.write_bytes(b"x")
    with pytest.raises(PathNotAllowedError):
        resolve_register_path(str(f), roots)
    with pytest.raises(PathNotAllowedError):
        resolve_register_path("../secret.m4a", roots)


def test_register_symlink_file_escape(roots: list[Path], tmp_path: Path) -> None:
    target = tmp_path / "secret.m4a"
    target.write_bytes(b"x")
    symlink_or_skip(roots[0] / "innocent.m4a", target)
    with pytest.raises(PathNotAllowedError):
        resolve_register_path("innocent.m4a", roots)


def test_register_missing_dir_and_extension(roots: list[Path]) -> None:
    with pytest.raises(NotFoundError):
        resolve_register_path("nope.m4a", roots)
    (roots[0] / "d.m4a").mkdir()
    with pytest.raises(InvalidPathError):
        resolve_register_path("d.m4a", roots)
    (roots[0] / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        resolve_register_path("notes.txt", roots)


# ------------------------------------------------------------------------------------ filenames
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("meeting.m4a", "meeting.m4a"),
        ("会议 录音.m4a", "会议 录音.m4a"),
        ("../../etc/passwd.m4a", "passwd.m4a"),
        ("C:\\Users\\x\\Desktop\\call.wav", "call.wav"),
        ("/abs/path/a.mp3", "a.mp3"),
        (".hidden.m4a", "hidden.m4a"),
        ("...m4a", "upload.m4a"),
        ("a\x00b\x1fc.m4a", "abc.m4a"),
        ("evil\u202egpj.m4a", "evilgpj.m4a"),
        ('a<b>c:d"e|f?g*h.wav', "a_b_c_d_e_f_g_h.wav"),
        ("CON.m4a", "_CON.m4a"),
        ("com1.wav", "_com1.wav"),
        ("lpt9.tar.mp3", "_lpt9.tar.mp3"),
        ("console.m4a", "console.m4a"),
        ("trailing. .m4a", "trailing.m4a"),
        ("  spaced   name .m4a", "spaced name.m4a"),
        ("", "upload"),
        ("..", "upload"),
        ("/", "upload"),
        ("noext", "noext"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_safe_filename_caps_length_and_keeps_extension() -> None:
    name = safe_filename("录" * 300 + ".m4a")
    assert name.endswith(".m4a")
    assert len(name.encode("utf-8")) <= storage.MAX_FILENAME_BYTES
    long_ascii = safe_filename("a" * 1000 + ".flac")
    assert long_ascii.endswith(".flac") and len(long_ascii) <= storage.MAX_FILENAME_BYTES


def test_collisions_get_numeric_suffix(tmp_path: Path) -> None:
    paths = []
    for _ in range(3):
        p, f = create_unique_file(tmp_path, "a.m4a")
        f.close()
        paths.append(p.name)
    assert paths == ["a.m4a", "a (1).m4a", "a (2).m4a"]


def test_concurrent_creation_never_shares_a_name(tmp_path: Path) -> None:
    names: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        p, f = create_unique_file(tmp_path, "same.wav")
        f.close()
        with lock:
            names.append(p.name)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(names)) == 16


def test_create_unique_does_not_follow_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    d = tmp_path / "d"
    d.mkdir()
    symlink_or_skip(d / "a.m4a", victim)
    p, f = create_unique_file(d, "a.m4a")
    f.write(b"new")
    f.close()
    assert p.name == "a (1).m4a"
    assert victim.read_text(encoding="utf-8") == "keep"


def test_move_into(tmp_path: Path) -> None:
    src = tmp_path / "tmpfile"
    src.write_bytes(b"data")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    (dst_dir / "x.m4a").write_bytes(b"old")
    got = storage.move_into(src, dst_dir, "../x.m4a")
    assert got == dst_dir / "x (1).m4a"
    assert got.read_bytes() == b"data"
    assert not src.exists()
    assert (dst_dir / "x.m4a").read_bytes() == b"old"


# ------------------------------------------------------------------------------------- sessions
def test_session_ids_validated(tmp_path: Path) -> None:
    for bad in ("", "..", "../../etc", "a" * 31, "a" * 33, "a/" + "b" * 30, "x" * 31 + "."):
        with pytest.raises(NotFoundError):
            storage.load_session(tmp_path, bad)


def test_session_lifecycle(
    tmp_path: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 10)
    data = bytes(range(25))
    s = storage.create_session(tmp_path, roots, filename="../a.wav", size=25, target_dir="in")
    assert storage.SESSION_ID_RE.fullmatch(s.session_id)
    assert (s.part_size, s.total_parts, s.filename) == (10, 3, "a.wav")
    manifest = json.loads(
        (tmp_path / "upload-sessions" / s.session_id / "session.json").read_text(encoding="utf-8")
    )
    assert manifest["target_dir"] == str(roots[0] / "in")
    assert [s.expected_part_size(n) for n in range(3)] == [10, 10, 5]
    with pytest.raises(InvalidUploadError):
        s.expected_part_size(3)
    with pytest.raises(InvalidUploadError):
        s.expected_part_size(-1)

    for n, chunk in enumerate((data[:10], data[10:20], data[20:])):
        tmp, f = storage.open_part_writer(tmp_path, s, n)
        f.write(chunk)
        f.close()
        storage.commit_part(tmp_path, s, n, tmp)
    assert storage.missing_parts(tmp_path, s) == []
    stored, original = storage.complete_session(tmp_path, roots, s.session_id)
    assert stored == roots[0] / "in" / "a.wav"
    assert stored.read_bytes() == data
    assert original == "a.wav"
    assert not (tmp_path / "upload-sessions" / s.session_id).exists()
    with pytest.raises(NotFoundError):
        storage.load_session(tmp_path, s.session_id)


def test_session_missing_parts(
    tmp_path: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 10)
    s = storage.create_session(tmp_path, roots, filename="a.wav", size=25, target_dir="")
    with pytest.raises(InvalidUploadError, match="0, 1, 2"):
        storage.complete_session(tmp_path, roots, s.session_id)
    # The lock is released after a failed complete, so the session can still be finished.
    for n, chunk in enumerate((b"a" * 10, b"b" * 10, b"c" * 5)):
        tmp, f = storage.open_part_writer(tmp_path, s, n)
        f.write(chunk)
        f.close()
        storage.commit_part(tmp_path, s, n, tmp)
    stored, _ = storage.complete_session(tmp_path, roots, s.session_id)
    assert stored.read_bytes() == b"a" * 10 + b"b" * 10 + b"c" * 5


def test_session_validation(tmp_path: Path, roots: list[Path]) -> None:
    with pytest.raises(TooLargeError):
        storage.create_session(
            tmp_path, roots, filename="a.wav", size=MAX_UPLOAD_SIZE + 1, target_dir=""
        )
    with pytest.raises(UnsupportedFormatError):
        storage.create_session(tmp_path, roots, filename="a.txt", size=1, target_dir="")
    with pytest.raises(PathNotAllowedError):
        storage.create_session(tmp_path, roots, filename="a.wav", size=1, target_dir="../x")
    with pytest.raises(InvalidUploadError):
        storage.create_session(tmp_path, roots, filename="a.wav", size=0, target_dir="")


def test_exact_multiple_of_part_size(
    tmp_path: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 10)
    s = storage.create_session(tmp_path, roots, filename="a.wav", size=20, target_dir="")
    assert s.total_parts == 2
    assert s.expected_part_size(1) == 10


def test_stale_sessions_cleaned(tmp_path: Path, roots: list[Path]) -> None:
    old = storage.create_session(tmp_path, roots, filename="a.wav", size=5, target_dir="")
    fresh = storage.create_session(tmp_path, roots, filename="b.wav", size=5, target_dir="")
    manifest = tmp_path / "upload-sessions" / old.session_id / "session.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["created_at"] = time.time() - storage.SESSION_TTL_S - 10
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(NotFoundError):
        storage.load_session(tmp_path, old.session_id)
    assert storage.load_session(tmp_path, fresh.session_id).filename == "b.wav"

    # Lazy cleanup on the next init removes expired sessions without touching fresh ones.
    expired = storage.create_session(tmp_path, roots, filename="c.wav", size=5, target_dir="")
    m = tmp_path / "upload-sessions" / expired.session_id / "session.json"
    d = json.loads(m.read_text(encoding="utf-8"))
    d["created_at"] = 0
    m.write_text(json.dumps(d), encoding="utf-8")
    storage.create_session(tmp_path, roots, filename="d.wav", size=5, target_dir="")
    remaining = {p.name for p in (tmp_path / "upload-sessions").iterdir()}
    assert expired.session_id not in remaining
    assert fresh.session_id in remaining


def test_session_target_revalidated_on_complete(
    tmp_path: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "PART_SIZE", 10)
    s = storage.create_session(tmp_path, roots, filename="a.wav", size=3, target_dir="t")
    tmp, f = storage.open_part_writer(tmp_path, s, 0)
    f.write(b"abc")
    f.close()
    storage.commit_part(tmp_path, s, 0, tmp)
    # Replace the target with a symlink to outside the roots between init and complete.
    target = roots[0] / "t"
    target.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(target, outside, target_is_directory=True)
    with pytest.raises(PathNotAllowedError):
        storage.complete_session(tmp_path, roots, s.session_id)
    assert os.listdir(outside) == []


def test_stale_multipart_temp_files_cleaned(tmp_path: Path) -> None:
    old, f = storage.new_temp_file(tmp_path)
    f.close()
    fresh, f = storage.new_temp_file(tmp_path)
    f.close()
    past = time.time() - storage.SESSION_TTL_S - 10
    os.utime(old, (past, past))
    storage.cleanup_stale_sessions(tmp_path)
    assert not old.exists()
    assert fresh.exists()
