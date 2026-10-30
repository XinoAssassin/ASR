"""Single-process guard (ARCH_REVIEW §4-d), log redaction for a plain ``uvicorn server.app:app``
run (server review m4) and the settings response (§4-f / review n3, n6)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from server.app import LOCK_FILE, DataDirInUse, DataDirLock, create_app
from server.config import EnvSettings
from tests.conftest import API_KEY, ENV_VARS, ClientFactory, EnvFactory

BACKEND = Path(__file__).resolve().parents[1]
DASHSCOPE_KEY = "sk-env-1234567890abcd"
OVERRIDE_KEY = "sk-override-0987654321wxyz"


# ------------------------------------------------------------------------ single process
async def test_second_server_on_the_same_data_dir_refuses_to_start(env: EnvSettings) -> None:
    async with LifespanManager(create_app(env)):
        lock_file = env.data_dir / LOCK_FILE
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        with pytest.raises(DataDirInUse, match="another ASR server is already running"):
            async with LifespanManager(create_app(env)):
                pass  # pragma: no cover
    # Released on shutdown: the next start works.
    async with LifespanManager(create_app(env)):
        pass


def test_lock_is_held_across_processes(tmp_path: Path) -> None:
    holder = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from server.app import DataDirLock
        lock = DataDirLock(Path(sys.argv[1]))
        lock.acquire()
        print("locked", flush=True)
        sys.stdin.readline()
        lock.release()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder, str(tmp_path)],
        cwd=BACKEND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"
        with pytest.raises(DataDirInUse):
            DataDirLock(tmp_path).acquire()
    finally:
        assert proc.stdin is not None
        proc.stdin.write("\n")
        proc.stdin.flush()
        proc.wait(30)
    lock = DataDirLock(tmp_path)
    lock.acquire()  # free again once the other process let go (or died)
    lock.release()


# ---------------------------------------------------------------- m4: plain uvicorn logging
def test_plain_uvicorn_launch_redacts_logs(tmp_path: Path) -> None:
    """``uvicorn server.app:app`` (no ``asr serve``): uvicorn configures only its own loggers,
    then imports the app. Secrets must not reach stderr from ``server.*`` or uvicorn loggers."""
    script = textwrap.dedent(
        """
        import asyncio, logging, logging.config
        from uvicorn.config import LOGGING_CONFIG
        logging.config.dictConfig(LOGGING_CONFIG)  # what uvicorn does before importing the app
        from server import config
        config.ENV_FILES = ()
        from asgi_lifespan import LifespanManager
        import server.app
        secret = "https://x.oss.aliyuncs.com/a.wav?Signature=abc sk-0123456789abcdef"
        logging.getLogger("server.early").warning("before startup %s", secret)
        async def main():
            async with LifespanManager(server.app.app):
                logging.getLogger("server.jobs").warning("during %s", secret)
                logging.getLogger("uvicorn.error").warning("uvicorn %s", secret)
                logging.getLogger("asr.providers.cloud").info("info line %s", secret)
        asyncio.run(main())
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
    env.update(
        ASR_DATA_DIR=str(tmp_path / "data"),
        ASR_UPLOAD_ROOTS=str(tmp_path / "uploads"),
        ASR_MODEL_CACHE_DIR=str(tmp_path / "models"),
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr
    for line in ("before startup", "during", "uvicorn", "info line"):
        assert line in err, err
    assert "Signature=abc" not in err and "sk-0123456789abcdef" not in err


# ------------------------------------------------------------- §4-f / n6: env-only facts
async def test_settings_expose_env_only_facts_without_secrets(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    env = env_factory(
        ASR_API_KEY=API_KEY,
        DASHSCOPE_API_KEY=DASHSCOPE_KEY,
        ASR_TORCH_THREADS="4",
        ASR_LLM_MODEL="qwen-max",
    )
    async with make_client(env, headers={"X-API-Key": API_KEY}) as c:
        resp = await c.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    environment = body["environment"]
    assert environment["api_key_required"] is True
    assert environment["allow_unauthenticated_network"] is False
    assert environment["cors_origins"] == env.cors_origins
    assert environment["allowed_hosts"] == env.allowed_hosts
    assert environment["upload_roots"] == [str(r) for r in env.upload_roots]
    assert environment["data_dir"] == str(env.data_dir)
    assert environment["model_cache_dir"] == str(env.model_cache_dir)
    assert environment["torch_threads"] == 4 and environment["llm_model"] == "qwen-max"
    assert environment["decoder"] == "pyav" and environment["log_file"] is None
    assert body["env_only"]["environment.cors_origins"] == "ASR_CORS_ORIGINS"
    assert body["env_only"]["api_key_required"] == "ASR_API_KEY"
    for path in body["env_only"]:  # every listed path exists in the response
        node = body
        for part in path.split("."):
            node = node[part]
    assert body["cloud"]["api_key_source"] == "env"
    raw = resp.text
    assert API_KEY not in raw and DASHSCOPE_KEY not in raw


async def test_api_key_source_follows_the_override(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    async with make_client(env_factory()) as c:
        assert (await c.get("/api/settings")).json()["cloud"]["api_key_source"] is None
    env = env_factory(DASHSCOPE_API_KEY=DASHSCOPE_KEY)
    async with make_client(env) as c:
        put = await c.put("/api/settings", json={"cloud": {"api_key": OVERRIDE_KEY}})
        assert put.status_code == 200 and put.json()["cloud"]["api_key_source"] == "settings"
        assert OVERRIDE_KEY not in put.text
        cleared = (await c.put("/api/settings", json={"cloud": {"api_key": ""}})).json()
        # The env key applies again: still set, and the UI can say why.
        assert cleared["cloud"]["api_key_set"] is True
        assert cleared["cloud"]["api_key_source"] == "env"


# ----------------------------------------------------------------- n3: diarization model
@pytest.mark.parametrize(
    ("value", "status", "stored"),
    [
        ("campplus", 200, "campplus"),
        ("  campplus ", 200, "campplus"),
        ("", 200, None),
        ("qwen3-asr-0.6b", 422, None),  # an ASR model, not a diarization model
        ("no-such-model", 422, None),
    ],
)
async def test_diarize_model_is_validated(
    client: httpx.AsyncClient, value: str, status: int, stored: str | None
) -> None:
    resp = await client.put("/api/settings", json={"local": {"diarize_model": value}})
    assert resp.status_code == status, resp.text
    current = (await client.get("/api/settings")).json()
    assert current["local"]["diarize_model"] == stored
    if status == 422:
        assert "diarization" in json.dumps(resp.json())
