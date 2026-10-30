"""M8 operations: ``asr batch``, ``asr serve`` and server logging (redaction, JSON, file)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from asr import cli as core_cli
from asr.providers.base import AudioSource, JobOptions, ProviderError
from server import cli as server_cli
from server.logging_setup import configure_logging
from tests.test_jobs import FakeProvider, sample_transcript

runner = CliRunner()
LAN = "0.0.0.0"  # noqa: S104 - the bind address under test


class BatchProvider(FakeProvider):
    """Fails for files named ``bad*`` (``crash*``: an unexpected error); records each call."""

    def transcribe(self, source: AudioSource, options: JobOptions, progress):  # type: ignore[override]
        assert source.path is not None
        if source.path.name.startswith("bad"):
            self.calls.append((source, options))
            raise ProviderError("cannot decode")
        if source.path.name.startswith("crash"):
            self.calls.append((source, options))
            raise RuntimeError("model exploded")
        return super().transcribe(source, options, progress)


@pytest.fixture
def batch_provider(monkeypatch) -> BatchProvider:
    provider = BatchProvider()
    builds: list[Path] = []

    def builder(is_url: bool, asr_model: str | None, device: str):
        assert not is_url

        def build(work_dir: Path) -> BatchProvider:
            builds.append(work_dir)
            return provider

        return build

    monkeypatch.setattr(core_cli, "_local_builder", builder)
    provider.builds = builds  # type: ignore[attr-defined]
    return provider


def _audio(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"\0" * 64)


def test_batch_transcribes_dirs_skips_existing_and_reports_failures(batch_provider, tmp_path):
    src = tmp_path / "in"
    _audio(src, "a.wav", "b.m4a", "bad.mp3", "notes.txt")
    _audio(src / "sub", "c.flac")
    terms = tmp_path / "terms.txt"
    terms.write_text("Townfall | 汤泡\nKubernetes,5\n", encoding="utf-8")
    out = tmp_path / "out"

    result = runner.invoke(
        server_cli.app,
        [
            "batch",
            str(src),
            "-r",
            "--out-dir",
            str(out),
            "-f",
            "txt",
            "-f",
            "srt",
            "--terms-file",
            str(terms),
            "--term",
            "Vue:50",
        ],
    )
    assert result.exit_code == 1, result.output  # one failure
    assert sorted(p.name for p in out.iterdir()) == ["a.srt", "a.txt", "b.srt", "b.txt", "sub"]
    assert sorted(p.name for p in (out / "sub").iterdir()) == ["c.srt", "c.txt"]  # mirrored
    assert (out / "a.txt").read_text(encoding="utf-8").strip()
    assert "3 transcribed, 0 skipped, 1 failed" in result.output
    assert "bad.mp3: cannot decode" in result.output
    assert len(batch_provider.builds) == 1  # models load once for the whole batch
    options = batch_provider.calls[0][1]
    assert {t.term: t.weight for t in options.terms} == {"Vue": 50, "Townfall": 3, "Kubernetes": 5}
    assert options.terms[1].aliases == ["汤泡"]

    again = runner.invoke(
        server_cli.app, ["batch", str(src), "-r", "--out-dir", str(out), "-f", "txt", "-f", "srt"]
    )
    assert "0 transcribed, 3 skipped, 1 failed" in again.output


def test_batch_defaults_write_next_to_input(batch_provider, tmp_path):
    _audio(tmp_path, "m.wav")
    result = runner.invoke(server_cli.app, ["batch", str(tmp_path / "m.wav")])
    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert len(data["segments"]) == len(sample_transcript().segments)
    assert (tmp_path / "m.txt").is_file()


def test_batch_names_same_stem_siblings_and_mirrors_subdirs(batch_provider, tmp_path):
    src = tmp_path / "in"
    _audio(src, "talk.wav", "talk.mp4", "solo.wav")
    _audio(src / "a", "x.wav")
    _audio(src / "b", "x.wav")
    out = tmp_path / "out"
    result = runner.invoke(
        server_cli.app, ["batch", str(src), "-r", "--out-dir", str(out), "-f", "txt"]
    )
    assert result.exit_code == 0, result.output
    assert "5 transcribed" in result.output
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.txt"))
    assert written == ["a/x.txt", "b/x.txt", "solo.txt", "talk.mp4.txt", "talk.wav.txt"]

    # next to the inputs (no --out-dir): the same sibling rule
    result = runner.invoke(server_cli.app, ["batch", str(src), "-f", "txt"])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in src.glob("*.txt")) == ["solo.txt", "talk.mp4.txt", "talk.wav.txt"]


def test_batch_refuses_colliding_outputs_up_front(batch_provider, tmp_path):
    _audio(tmp_path / "a", "x.wav")
    _audio(tmp_path / "b", "x.wav")
    out = tmp_path / "out"
    args = ["batch", str(tmp_path / "a"), str(tmp_path / "b"), "--out-dir", str(out)]
    result = runner.invoke(server_cli.app, args)
    assert result.exit_code == 2 and "collision" in result.output
    assert batch_provider.calls == [] and not out.exists()


def test_batch_survives_unexpected_errors_and_unknown_duration(batch_provider, tmp_path):
    _audio(tmp_path, "crash.wav", "ok.wav")
    batch_provider.result = batch_provider.result.model_copy(update={"duration": None})
    result = runner.invoke(server_cli.app, ["batch", str(tmp_path), "-f", "txt"])
    assert result.exit_code == 1, result.output
    assert "1 transcribed, 0 skipped, 1 failed" in result.output
    assert "RuntimeError: model exploded" in result.output
    assert "? s audio" in result.output
    assert (tmp_path / "ok.txt").is_file()


def test_batch_rejects_bad_terms_file_and_empty_input(batch_provider, tmp_path):
    bad = tmp_path / "t.txt"
    bad.write_text("x,notanumber\n", encoding="utf-8")
    _audio(tmp_path, "m.wav")
    result = runner.invoke(server_cli.app, ["batch", str(tmp_path), "--terms-file", str(bad)])
    assert result.exit_code == 2 and "line 1" in result.output
    empty = tmp_path / "empty"
    empty.mkdir()
    assert runner.invoke(server_cli.app, ["batch", str(empty)]).exit_code == 2


def test_serve_runs_one_uvicorn_worker_with_our_logging(
    monkeypatch, tmp_path, restore_root_logging
):
    calls: list[dict] = []
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setenv("ASR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASR_LOG_FILE", str(tmp_path / "logs" / "asr.log"))
    from server.config import get_env

    get_env.cache_clear()
    args = ["serve", "--host", LAN, "--port", "8123", "--forwarded-allow-ips", "10.0.0.1"]
    refused = runner.invoke(server_cli.app, args)  # LAN bind without a key: refused
    assert refused.exit_code == 2 and "refusing to listen" in refused.output and not calls
    monkeypatch.setenv("ASR_ALLOW_UNAUTHENTICATED_NETWORK", "1")
    get_env.cache_clear()
    result = runner.invoke(server_cli.app, args)
    assert result.exit_code == 0, result.output
    (call,) = calls
    assert call["app"] == "server.app:app"
    assert (call["host"], call["port"], call["workers"]) == (LAN, 8123, 1)
    assert call["log_config"] is None
    assert call["proxy_headers"] is True and call["forwarded_allow_ips"] == "10.0.0.1"
    assert "ASR_API_KEY is empty" in result.output  # LAN bind without a key
    assert (tmp_path / "logs" / "asr.log").exists()


def test_serve_reports_config_errors_without_secrets(monkeypatch):
    monkeypatch.setenv("ASR_CORS_ORIGINS", "*")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-abcdefabcdef12345678")
    from server.config import get_env

    get_env.cache_clear()
    result = runner.invoke(server_cli.app, ["serve"])
    assert result.exit_code == 2
    assert "sk-abcdef" not in result.output


QUIETED = ("httpx", "httpcore", "modelscope", "urllib3", "multipart")


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {name: logging.getLogger(name).level for name in QUIETED}
    yield
    for h in [h for h in root.handlers if getattr(h, "_asr_handler", False)]:
        root.removeHandler(h)
        h.close()
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)
    for name, lvl in levels.items():
        logging.getLogger(name).setLevel(lvl)


def test_logging_redacts_and_writes_json(tmp_path, restore_root_logging):
    log_file = tmp_path / "asr.log"
    configure_logging("INFO", "json", log_file)
    configure_logging("INFO", "json", log_file)  # idempotent: one set of handlers
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_asr_handler", False)]
    assert len(ours) == 2

    log = logging.getLogger("server.test")
    log.info("job %s started", "j1", extra={"job_id": "j1"})
    log.warning(
        "calling https://x.oss.aliyuncs.com/a.wav?Signature=abc with key sk-0123456789abcdef"
    )
    try:
        raise RuntimeError("Authorization: Bearer secret-token-123")
    except RuntimeError:
        log.exception("boom")
    log.debug("hidden")
    for h in ours:
        h.flush()

    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["msg"] == "job j1 started"
    assert lines[0]["job_id"] == "j1" and lines[0]["level"] == "INFO"
    text = log_file.read_text(encoding="utf-8")
    assert "Signature=abc" not in text and "sk-0123456789abcdef" not in text
    assert "secret-token-123" not in text and "RuntimeError" in text
    assert "hidden" not in text


def test_logging_text_format_and_levels(tmp_path, restore_root_logging):
    log_file = tmp_path / "t.log"
    configure_logging("warning", "text", log_file)
    logging.getLogger("asr.x").info("quiet")
    logging.getLogger("asr.x").warning("loud")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "loud" in text and "quiet" not in text and "WARNING" in text
