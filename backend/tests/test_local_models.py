"""Local model registry and the explicit download command (AGENTS.md rule 6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from asr.cli import app
from asr.providers.local import models


def test_model_dir_never_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> None:
        raise AssertionError("download attempted")

    monkeypatch.setattr(models, "download", boom)
    with pytest.raises(models.ModelNotDownloaded, match=r"asr models download qwen3-asr-0\.6b"):
        models.model_dir(tmp_path, "qwen3-asr-0.6b")


def test_model_dir_after_marker(tmp_path: Path) -> None:
    path = models.model_path(tmp_path, "fsmn-vad")
    path.mkdir(parents=True)
    assert not models.is_downloaded(tmp_path, "fsmn-vad")
    (path / "configuration.json").write_text("{}", encoding="utf-8")
    assert not models.is_downloaded(tmp_path, "fsmn-vad")  # config alone is not enough
    (path / "model.pt").write_bytes(b"w")
    partial = path / ".cache" / "huggingface" / "download" / "x.incomplete"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"")
    assert not models.is_downloaded(tmp_path, "fsmn-vad")  # interrupted download
    partial.unlink()
    assert (
        models.model_dir(tmp_path, "fsmn-vad")
        == tmp_path / "modelscope" / models.spec("fsmn-vad").repo_id
    )


def test_unknown_model() -> None:
    with pytest.raises(KeyError, match="unknown model"):
        models.spec("whisper")


def test_cli_models_list_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_MODEL_CACHE_DIR", str(tmp_path))
    calls: list[str] = []

    def fake_download(cache: Path, key: str, *, mirror: bool = False) -> Path:
        calls.append(key)
        path = models.model_path(cache, key)
        path.mkdir(parents=True, exist_ok=True)
        (path / models.spec(key).marker).write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(models, "download", fake_download)
    runner = CliRunner()
    assert runner.invoke(app, ["models", "download", "nope"]).exit_code == 2
    result = runner.invoke(app, ["models", "download", "fsmn-vad", "fsmn-vad"])
    assert result.exit_code == 0, result.output
    assert calls == ["fsmn-vad"]  # second request sees it already downloaded
    listing = runner.invoke(app, ["models", "list"]).stdout
    assert "fsmn-vad" in listing and "downloaded" in listing


def test_resolve_dtype(monkeypatch: pytest.MonkeyPatch) -> None:
    from asr.providers.local import device

    monkeypatch.delenv("ASR_LOCAL_DTYPE", raising=False)
    monkeypatch.setattr(device, "cpu_features", lambda: device.CpuFeatures("AVX512", True))
    assert device.resolve_dtype("auto") == "bfloat16"
    monkeypatch.setattr(device, "cpu_features", lambda: device.CpuFeatures("AVX2", False))
    assert device.resolve_dtype() == "float32"
    monkeypatch.setenv("ASR_LOCAL_DTYPE", "bfloat16")
    assert device.resolve_dtype() == "bfloat16"
    assert device.resolve_dtype("float32") == "float32"  # explicit argument wins over env
    with pytest.raises(ValueError, match="unknown dtype"):
        device.resolve_dtype("int8")
