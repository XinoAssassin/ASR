"""Deploy-agent findings: .env values reach the core library and the CLI; clean JSON logs."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from server import config
from server.config import (
    ConfigError,
    apply_core_environ,
    core_environ,
    load_env,
    load_env_files_into_environ,
)
from server.logging_setup import JsonFormatter
from tests.conftest import ClientFactory, EnvFactory

CORE_VARS = ("ASR_DECODER", "ASR_TORCH_THREADS", "ASR_LOCAL_DTYPE")


@pytest.fixture
def clean_core_env() -> Iterator[None]:
    """Unset the core variables and restore them exactly afterwards (the app lifespan writes
    ``os.environ`` directly, which monkeypatch would not undo)."""
    saved = {name: os.environ.pop(name, None) for name in CORE_VARS}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_threads_and_dtype_are_validated_settings(
    env_factory: EnvFactory, clean_core_env: None
) -> None:
    env = env_factory(ASR_TORCH_THREADS="8", ASR_LOCAL_DTYPE=" BFloat16 ")
    assert env.torch_threads == 8 and env.local_dtype == "bfloat16"
    env = env_factory(ASR_TORCH_THREADS="", ASR_LOCAL_DTYPE="")
    assert env.torch_threads is None and env.local_dtype == "auto"
    for name, bad in (
        ("ASR_TORCH_THREADS", "0"),
        ("ASR_TORCH_THREADS", "x"),
        ("ASR_LOCAL_DTYPE", "int4"),
    ):
        os.environ[name] = bad
        with pytest.raises(ConfigError, match=name):
            load_env(())
        del os.environ[name]


def test_dotenv_values_reach_the_core_library(
    tmp_path: Path,
    env_factory: EnvFactory,
    monkeypatch: pytest.MonkeyPatch,
    clean_core_env: None,
) -> None:
    env_factory()  # hermetic base (clears every ASR_* variable)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ASR_TORCH_THREADS=6\nASR_LOCAL_DTYPE=float32\nASR_DECODER=imageio\n", encoding="utf-8"
    )
    env = load_env((dotenv,))
    assert core_environ(env) == {
        "ASR_DECODER": "imageio",
        "ASR_TORCH_THREADS": "6",
        "ASR_LOCAL_DTYPE": "float32",
    }
    for name in CORE_VARS:
        assert name not in os.environ  # only in the .env file so far
    apply_core_environ(env)
    assert os.environ["ASR_TORCH_THREADS"] == "6"
    from asr.providers.local.device import resolve_dtype

    assert resolve_dtype() == "float32"

    apply_core_environ(env.model_copy(update={"torch_threads": None}))
    assert "ASR_TORCH_THREADS" not in os.environ


async def test_app_lifespan_applies_core_environ(
    env_factory: EnvFactory, make_client: ClientFactory, clean_core_env: None
) -> None:
    env = env_factory(ASR_TORCH_THREADS="3", ASR_LOCAL_DTYPE="bfloat16")
    os.environ.pop("ASR_TORCH_THREADS")  # as if it came from a .env file only
    os.environ.pop("ASR_LOCAL_DTYPE")
    async with make_client(env):
        assert os.environ["ASR_TORCH_THREADS"] == "3"
        assert os.environ["ASR_LOCAL_DTYPE"] == "bfloat16"


def test_cli_loads_dotenv_and_real_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root_env, backend_env = tmp_path / "root.env", tmp_path / "backend.env"
    root_env.write_text(
        "ASR_MODEL_CACHE_DIR=/from/root\nM11_TEST_SECRET=s3cret-value\nM11_TEST_REAL=file\n",
        encoding="utf-8",
    )
    backend_env.write_text(f"ASR_MODEL_CACHE_DIR={tmp_path / 'cache'}\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILES", (root_env, backend_env, tmp_path / "missing.env"))
    for name in ("ASR_MODEL_CACHE_DIR", "M11_TEST_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("M11_TEST_REAL", "real")

    from asr.cli import STARTUP_HOOKS
    from server import cli as server_cli

    assert server_cli.load_dotenv_files in STARTUP_HOOKS
    try:
        result = CliRunner().invoke(server_cli.app, ["models", "list"])
        assert result.exit_code == 0, result.output
        assert os.environ["ASR_MODEL_CACHE_DIR"] == str(tmp_path / "cache")  # later file wins
        assert os.environ["M11_TEST_REAL"] == "real"  # real env wins over the file
        assert str(tmp_path / "cache") in result.output  # `models list` used the .env cache
        assert "s3cret-value" not in result.output + capsys.readouterr().err
    finally:
        for name in ("ASR_MODEL_CACHE_DIR", "M11_TEST_SECRET"):
            os.environ.pop(name, None)


def test_load_env_files_returns_names_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / ".env"
    f.write_text("M11_A=1\nM11_B\n", encoding="utf-8")  # a bare name has no value: skipped
    monkeypatch.delenv("M11_A", raising=False)
    try:
        assert load_env_files_into_environ((f,)) == ["M11_A"]
        assert os.environ["M11_A"] == "1" and "M11_B" not in os.environ
    finally:
        os.environ.pop("M11_A", None)


def test_json_log_skips_uvicorn_color_message() -> None:
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started", None, None)
    record.color_message = "\x1b[32mStarted\x1b[0m"
    record.job_id = "j1"
    entry = json.loads(JsonFormatter().format(record))
    assert "color_message" not in entry and entry["job_id"] == "j1"
    assert "\x1b" not in json.dumps(entry)
