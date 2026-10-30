"""Online settings: config-file layer, precedence (settings > config > env > default), the LLM
provider (DashScope or OpenAI-compatible), test-llm, and secrets never leaking (rule 3)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from asr.correct.llm import (
    LlmCorrector,
    chat_url,
    check_llm,
    normalize_base_url,
    parse_json_text,
)
from asr.providers.base import ProviderError, Segment, Speaker
from asr.summarize import Summarizer
from server import config_file as cfgfile
from server.config import ConfigError, EnvSettings, effective_settings
from tests.conftest import ClientFactory, EnvFactory

DS = "https://dashscope.aliyuncs.com"
DS_CHAT = f"{DS}/compatible-mode/v1/chat/completions"
WS = "https://ws1.cn-beijing.maas.aliyuncs.com"
LAN = "http://llm.test:8080/v1"
LAN_CHAT = f"{LAN}/chat/completions"

ENV_KEY = "sk-envkey-0000000000aaaa"
CFG_KEY = "sk-cfgkey-1111111111bbbb"
UI_KEY = "sk-uikey-22222222222cccc"
LLM_KEY = "local-llm-key-3333333dddd"
SECRETS = (ENV_KEY, CFG_KEY, UI_KEY, LLM_KEY)

ONLINE_KEYS = (
    "cloud.endpoint",
    "cloud.model",
    "cloud.file_transfer",
    "cloud.api_key",
    "llm.provider",
    "llm.endpoint",
    "llm.model",
    "llm.api_key",
)


def write_config(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def default_config(env: EnvSettings) -> Path:
    return env.data_dir / "config.toml"


def chat_ok(content: Any = None, model: str | None = None) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content or {"ok": True})
    body: dict[str, Any] = {"choices": [{"message": {"content": text}}]}
    if model:
        body["model"] = model
    return httpx.Response(200, json=body)


def no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# ------------------------------------------------------------------------------- core helpers
def test_normalize_base_url() -> None:
    assert normalize_base_url(" http://192.168.2.10:8080/v1/ ") == "http://192.168.2.10:8080/v1"
    assert normalize_base_url("https://h.example/v1/chat/completions") == "https://h.example/v1"
    assert normalize_base_url("http://localhost:11434") == "http://localhost:11434"
    for bad in (
        "",
        "ftp://h/v1",
        "h/v1",
        "http://user:pw@h/v1",
        "http://h/v1?key=x",
        "http://h/v1#x",
        "http://h /v1",
        "http://h:99999/v1",
        "http:///v1",
    ):
        with pytest.raises(ValueError) as exc:
            normalize_base_url(bad)
        assert bad.strip() == "" or bad not in str(exc.value)


def test_parse_json_text() -> None:
    assert parse_json_text('{"a": 1}') == {"a": 1}
    assert parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('<think>hmm {"x": 0}</think>\n{"a": 1}') == {"a": 1}
    assert parse_json_text('Here you go: {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_text("no json at all")


def test_chat_url_per_provider() -> None:
    assert chat_url("k", DS) == DS_CHAT
    assert chat_url("", LAN, "openai") == LAN_CHAT  # key optional for openai
    with pytest.raises(ProviderError, match="not configured"):
        chat_url("", DS)
    with pytest.raises(ProviderError, match="not configured"):
        chat_url("", "", "openai")
    with pytest.raises(ProviderError, match="is invalid"):
        chat_url("k", "ftp://x", "openai")
    with pytest.raises(ProviderError, match="allow-list"):
        chat_url("k", LAN, "dashscope")


# ------------------------------------------------------------------------- config file parse
def test_parse_reports_problems_without_values() -> None:
    state = cfgfile.parse(
        f"""
[cloud]
api_key = "{CFG_KEY}"
endpoint = "https://evil.example.com"
model = 42
colour = "blue"
[llm]
provider = "anthropic"
endpoint = "http://user:{LLM_KEY}@h/v1"
api_key = "has space {LLM_KEY}"
[extra]
x = 1
""".encode(),
        "/x/config.toml",
    )
    info = state.info
    assert info.loaded and info.path == "/x/config.toml"
    assert dict(state.values) == {"cloud.api_key": CFG_KEY}
    assert info.error is not None
    for key in ("cloud.endpoint", "cloud.model", "llm.provider", "llm.endpoint", "llm.api_key"):
        assert key in info.error
    assert any("cloud.colour" in w for w in info.warnings)
    assert any("[extra]" in w for w in info.warnings)
    no_secrets(info.error + " ".join(info.warnings) + repr(state))
    assert "evil.example.com" not in info.error


def test_parse_malformed_toml_reports_position_only() -> None:
    state = cfgfile.parse(f'[cloud]\napi_key = "{CFG_KEY}\n'.encode(), "/x/c.toml")
    assert not state.info.loaded and state.values == {}
    assert state.info.error is not None and "not valid TOML" in state.info.error
    assert "line 2" in state.info.error
    no_secrets(state.info.error)
    assert "not valid UTF-8" in (cfgfile.parse(b"\xff\xfe", "/x").info.error or "")
    assert "larger" in (cfgfile.parse(b"#" * (cfgfile.MAX_BYTES + 1), "/x").info.error or "")


def test_empty_values_are_unset() -> None:
    state = cfgfile.parse(b'[llm]\nendpoint = ""\napi_key = ""\nmodel = " "\n', "/x")
    assert state.values == {} and state.info.error is None


# ------------------------------------------------------------------------------- precedence
CLOUD_LAYERS: dict[str, tuple[str, str, str, str]] = {
    # key: (env var, env value, config value, settings value)
    "cloud.endpoint": ("DASHSCOPE_ENDPOINT", WS, "https://cn-beijing.maas.qianwenaiapi.com", DS),
    "cloud.model": ("ASR_CLOUD_MODEL", "m-env", "m-cfg", "m-ui"),
    "cloud.file_transfer": ("ASR_CLOUD_FILE_TRANSFER", "public_url", "temp_upload", "public_url"),
    "cloud.api_key": ("DASHSCOPE_API_KEY", ENV_KEY, CFG_KEY, UI_KEY),
    "llm.model": ("ASR_LLM_MODEL", "llm-env", "llm-cfg", "llm-ui"),
}


def _effective_value(body: dict[str, Any], key: str) -> Any:
    section, name = key.split(".")
    if name == "api_key":
        return body[section]["api_key_masked"]
    return body[section][name]


@pytest.mark.parametrize("key", list(CLOUD_LAYERS))
async def test_precedence_matrix(
    key: str, env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    from server.config import mask_secret

    var, env_value, cfg_value, ui_value = CLOUD_LAYERS[key]
    section, name = key.split(".")

    def shown(value: str) -> str | None:
        return mask_secret(value) if name == "api_key" else value

    # default
    env = env_factory()
    async with make_client(env) as c:
        body = (await c.get("/api/settings")).json()
        assert body["sources"][key] == "default"
    # env
    env = env_factory(**{var: env_value})
    async with make_client(env) as c:
        body = (await c.get("/api/settings")).json()
        assert body["sources"][key] == "env"
        assert _effective_value(body, key) == shown(env_value)
        # config beats env
        write_config(default_config(env), f'[{section}]\n{name} = "{cfg_value}"\n')
        body = (await c.get("/api/settings")).json()
        assert body["sources"][key] == "config"
        assert _effective_value(body, key) == shown(cfg_value)
        # settings beat config
        body = (await c.put("/api/settings", json={section: {name: ui_value}})).json()
        assert body["sources"][key] == "settings"
        assert _effective_value(body, key) == shown(ui_value)
        # "" clears the override: config applies again
        body = (await c.put("/api/settings", json={section: {name: ""}})).json()
        assert body["sources"][key] == "config"
        assert _effective_value(body, key) == shown(cfg_value)
        # file removed: env applies again
        default_config(env).unlink()
        body = (await c.get("/api/settings")).json()
        assert body["sources"][key] == "env"
        assert body["config_file"] == {
            "path": None,
            "loaded": False,
            "error": None,
            "warnings": [],
        }
        no_secrets((await c.get("/api/settings")).text)


async def test_cloud_key_source_config(env_factory: EnvFactory, make_client: ClientFactory) -> None:
    env = env_factory(DASHSCOPE_API_KEY=ENV_KEY)
    write_config(default_config(env), f'[cloud]\napi_key = "{CFG_KEY}"\n')
    async with make_client(env) as c:
        body = (await c.get("/api/settings")).json()
    assert body["cloud"]["api_key_source"] == "config"
    assert body["cloud"]["api_key_masked"] == "sk-****bbbb"
    assert body["config_file"]["loaded"] is True
    assert body["config_file"]["path"] == str(default_config(env))
    # The cloud ASR provider and test-cloud get the config value too.
    eff = effective_settings(env, {})
    assert eff.cloud_api_key.get_secret_value() == CFG_KEY


async def test_explicit_config_path(
    env_factory: EnvFactory, make_client: ClientFactory, tmp_path: Path
) -> None:
    path = tmp_path / "etc" / "asr.toml"
    env = env_factory(ASR_CONFIG_FILE=str(path))
    async with make_client(env) as c:
        body = (await c.get("/api/settings")).json()
        assert body["config_file"]["path"] == str(path.resolve())
        assert body["config_file"]["loaded"] is False
        assert body["config_file"]["error"] == "config file not found"
        write_config(path, '[cloud]\nmodel = "from-explicit"\n')
        body = (await c.get("/api/settings")).json()
        assert body["cloud"]["model"] == "from-explicit"
        assert body["config_file"]["error"] is None


async def test_config_reloaded_when_file_changes(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    env = env_factory()
    path = write_config(default_config(env), '[cloud]\nmodel = "first"\n')
    async with make_client(env) as c:
        assert (await c.get("/api/settings")).json()["cloud"]["model"] == "first"
        st = path.stat()
        path.write_text('[cloud]\nmodel = "second"\n', encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        assert (await c.get("/api/settings")).json()["cloud"]["model"] == "second"
        # Unchanged file: served from the cache (no re-parse).
        calls: list[bytes] = []
        real = cfgfile.parse

        def counting(raw: bytes, shown: str) -> cfgfile.ConfigFileState:
            calls.append(raw)
            return real(raw, shown)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfgfile, "parse", counting)
            await c.get("/api/settings")
        assert calls == []


async def test_malformed_file_does_not_break_the_server(
    env_factory: EnvFactory, make_client: ClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = env_factory(ASR_CLOUD_MODEL="env-model")
    write_config(default_config(env), f'[cloud]\napi_key = "{CFG_KEY}\nmodel = "x"\n')
    async with make_client(env) as c:
        resp = await c.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_file"]["loaded"] is False
    assert "not valid TOML" in body["config_file"]["error"]
    assert body["cloud"]["model"] == "env-model"
    assert body["cloud"]["api_key_set"] is False
    no_secrets(resp.text + caplog.text)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
async def test_permission_warning(env_factory: EnvFactory, make_client: ClientFactory) -> None:
    env = env_factory()
    path = write_config(default_config(env), f'[cloud]\napi_key = "{CFG_KEY}"\n', mode=0o644)
    async with make_client(env) as c:
        warnings = (await c.get("/api/settings")).json()["config_file"]["warnings"]
        assert any("chmod 600" in w for w in warnings)
        os.chmod(path, 0o600)
        assert (await c.get("/api/settings")).json()["config_file"]["warnings"] == []


async def test_config_secrets_never_logged_or_returned(
    env_factory: EnvFactory, make_client: ClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = env_factory()
    write_config(
        default_config(env),
        f"""[cloud]
api_key = "{CFG_KEY}"
[llm]
provider = "openai"
endpoint = "{LAN}"
api_key = "{LLM_KEY}"
nonsense = "{UI_KEY}"
""",
    )
    async with make_client(env) as c:
        texts = [(await c.get("/api/settings")).text, (await c.get("/openapi.json")).text]
        with respx.mock(assert_all_mocked=True) as router:
            router.post(LAN_CHAT).mock(return_value=httpx.Response(500, text=LLM_KEY))
            texts.append((await c.post("/api/settings/test-llm")).text)
    body = json.loads(texts[0])
    assert body["llm"]["api_key_masked"] == "****dddd"
    assert body["sources"]["llm.api_key"] == "config"
    for text in [*texts, caplog.text]:
        no_secrets(text)


# --------------------------------------------------------------------------------- llm layers
async def test_llm_defaults_inherit_cloud(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    async with make_client(env_factory(DASHSCOPE_API_KEY=ENV_KEY, DASHSCOPE_ENDPOINT=WS)) as c:
        llm = (await c.get("/api/settings")).json()["llm"]
    assert llm == {
        "provider": "dashscope",
        "endpoint": WS,
        "model": "qwen-plus",
        "api_key_set": True,
        "api_key_masked": "sk-****aaaa",
    }


async def test_llm_env_layer(env_factory: EnvFactory, make_client: ClientFactory) -> None:
    env = env_factory(
        DASHSCOPE_API_KEY=ENV_KEY,
        ASR_LLM_PROVIDER="openai",
        ASR_LLM_ENDPOINT=LAN + "/",
        ASR_LLM_API_KEY=LLM_KEY,
    )
    async with make_client(env) as c:
        body = (await c.get("/api/settings")).json()
    assert body["llm"]["provider"] == "openai" and body["llm"]["endpoint"] == LAN
    assert body["llm"]["api_key_masked"] == "****dddd"
    assert {body["sources"][k] for k in ("llm.provider", "llm.endpoint", "llm.api_key")} == {"env"}

    with pytest.raises(ConfigError, match=r"llm_provider|ASR_LLM_PROVIDER"):
        env_factory(ASR_LLM_PROVIDER="bogus")
    # An env endpoint that does not suit the provider is ignored with a warning.
    eff = effective_settings(env_factory(ASR_LLM_ENDPOINT=LAN), {})
    assert eff.llm_endpoint == DS and eff.sources["llm.endpoint"] == "default"
    assert any("llm.endpoint" in w for w in eff.config_file.warnings)


async def test_openai_never_gets_the_dashscope_key(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    env = env_factory(DASHSCOPE_API_KEY=ENV_KEY)
    async with make_client(env) as c:
        body = (
            await c.put("/api/settings", json={"llm": {"provider": "openai", "endpoint": LAN}})
        ).json()
        assert body["llm"]["api_key_set"] is False and body["llm"]["endpoint"] == LAN
        with respx.mock(assert_all_mocked=True) as router:
            route = router.post(LAN_CHAT).mock(return_value=chat_ok())
            resp = (await c.post("/api/settings/test-llm")).json()
        assert resp["ok"] is True
        assert "authorization" not in route.calls[0].request.headers


async def test_config_key_not_sent_to_an_endpoint_set_in_settings(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    env = env_factory()
    write_config(
        default_config(env),
        f'[llm]\nprovider = "openai"\nendpoint = "{LAN}"\napi_key = "{LLM_KEY}"\n',
    )
    other = "http://other.test/v1"
    async with make_client(env) as c:
        assert (await c.get("/api/settings")).json()["llm"]["api_key_set"] is True
        body = (await c.put("/api/settings", json={"llm": {"endpoint": other}})).json()
        assert body["llm"]["endpoint"] == other and body["llm"]["api_key_set"] is False
        assert any("llm.api_key" in w for w in body["config_file"]["warnings"])
        with respx.mock(assert_all_mocked=True) as router:
            route = router.post(f"{other}/chat/completions").mock(return_value=httpx.Response(401))
            resp = (await c.post("/api/settings/test-llm")).json()
        assert "authorization" not in route.calls[0].request.headers
        assert resp["ok"] is False and "llm.api_key" in resp["message"]
        # A key given together with the endpoint is used.
        body = (
            await c.put("/api/settings", json={"llm": {"endpoint": other, "api_key": UI_KEY}})
        ).json()
        assert body["llm"]["api_key_masked"] == "sk-****cccc"
        # Clearing the endpoint override: back to the config endpoint. The Settings key was
        # bound to `other`, so it is cleared and the config key applies again.
        body = (await c.put("/api/settings", json={"llm": {"endpoint": ""}})).json()
        assert body["llm"]["endpoint"] == LAN
        assert body["sources"]["llm.api_key"] == "config"


async def test_stored_llm_key_cleared_when_provider_switches(
    env_factory: EnvFactory, make_client: ClientFactory
) -> None:
    env = env_factory()
    write_config(default_config(env), f'[llm]\nendpoint = "{LAN}"\n')
    async with make_client(env) as c:
        body = (await c.put("/api/settings", json={"llm": {"api_key": UI_KEY}})).json()
        # dashscope: the config endpoint does not suit it, the cloud endpoint is used
        assert body["llm"]["provider"] == "dashscope" and body["llm"]["endpoint"] == DS
        assert body["llm"]["api_key_masked"] == "sk-****cccc"
        assert any("llm.endpoint" in w for w in body["config_file"]["warnings"])
        body = (await c.put("/api/settings", json={"llm": {"provider": "openai"}})).json()
        assert body["llm"]["endpoint"] == LAN
        assert body["llm"]["api_key_set"] is False  # the DashScope-bound key was dropped
        # Model-only changes keep a key.
        await c.put("/api/settings", json={"llm": {"api_key": LLM_KEY}})
        body = (await c.put("/api/settings", json={"llm": {"model": "qwen3:8b"}})).json()
        assert body["llm"]["api_key_set"] is True and body["llm"]["model"] == "qwen3:8b"


@pytest.mark.parametrize(
    "body",
    [
        {"llm": {"provider": "anthropic"}},
        {"llm": {"provider": "openai", "endpoint": "ftp://h/v1"}},
        {"llm": {"provider": "openai", "endpoint": "http://user:pw@h/v1"}},
        {"llm": {"provider": "openai", "endpoint": "http://h/v1?key=1"}},
        {"llm": {"endpoint": "http://192.168.2.10:8080/v1"}},  # provider dashscope
        {"llm": {"provider": "dashscope", "endpoint": "https://evil.example.com"}},
        {"llm": {"model": "m" * 201}},
        {"llm": {"api_key": "sk-a b"}},
        {"cloud": {"endpoint": "https://evil.example.com"}},
        {"cloud": {"file_transfer": "oss"}},
    ],
)
async def test_llm_validation(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    before = (await client.get("/api/settings")).json()
    resp = await client.put("/api/settings", json=body)
    assert resp.status_code == 422
    assert "sk-a b" not in resp.text and "pw@" not in resp.text
    assert (await client.get("/api/settings")).json() == before


async def test_llm_update_round_trip(client: httpx.AsyncClient) -> None:
    body = (
        await client.put(
            "/api/settings",
            json={
                "llm": {
                    "provider": "openai",
                    "endpoint": "http://192.168.2.10:8080/v1/",
                    "model": "qwen3-8b",
                    "api_key": LLM_KEY,
                }
            },
        )
    ).json()
    assert body["llm"] == {
        "provider": "openai",
        "endpoint": "http://192.168.2.10:8080/v1",
        "model": "qwen3-8b",
        "api_key_set": True,
        "api_key_masked": "****dddd",
    }
    assert all(body["sources"][f"llm.{k}"] == "settings" for k in ("provider", "endpoint"))
    # Clear everything: back to the defaults.
    body = (
        await client.put(
            "/api/settings",
            json={"llm": {"provider": "", "endpoint": "", "model": "", "api_key": ""}},
        )
    ).json()
    assert body["llm"]["provider"] == "dashscope" and body["llm"]["endpoint"] == DS
    assert all(body["sources"][k] == "default" for k in ONLINE_KEYS)


async def test_dashscope_llm_endpoint_allowlisted(client: httpx.AsyncClient) -> None:
    body = (await client.put("/api/settings", json={"llm": {"endpoint": WS + "/"}})).json()
    assert body["llm"]["endpoint"] == WS and body["cloud"]["endpoint"] == DS


# ---------------------------------------------------------------------------------- test-llm
async def test_test_llm_dashscope(env_factory: EnvFactory, make_client: ClientFactory) -> None:
    async with make_client(env_factory(DASHSCOPE_API_KEY=ENV_KEY)) as c:
        with respx.mock(assert_all_mocked=True) as router:
            route = router.post(DS_CHAT).mock(return_value=chat_ok(model="qwen-plus-2026"))
            resp = (await c.post("/api/settings/test-llm")).json()
    assert resp == {"ok": True, "message": "The LLM answered", "model": "qwen-plus-2026"}
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {ENV_KEY}"
    sent = json.loads(request.content)
    assert sent["max_tokens"] <= 32 and sent["response_format"] == {"type": "json_object"}


async def test_test_llm_without_dashscope_key_makes_no_call(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_mocked=True) as router:
        resp = (await client.post("/api/settings/test-llm")).json()
        assert not router.calls
    assert resp["ok"] is False and "not configured" in resp["message"]
    assert resp["model"] is None


async def test_test_llm_openai_not_configured(client: httpx.AsyncClient) -> None:
    await client.put("/api/settings", json={"llm": {"provider": "openai"}})
    resp = await client.post("/api/settings/test-llm")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "message": "LLM endpoint is not configured", "model": None}


async def test_test_llm_openai_with_key_and_json_fallback(client: httpx.AsyncClient) -> None:
    await client.put(
        "/api/settings",
        json={"llm": {"provider": "openai", "endpoint": LAN, "api_key": LLM_KEY, "model": "q"}},
    )
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(LAN_CHAT).mock(
            side_effect=[
                httpx.Response(400, json={"error": {"message": "response_format not supported"}}),
                chat_ok('```json\n{"ok": true}\n```'),
            ]
        )
        resp = (await client.post("/api/settings/test-llm")).json()
    assert resp["ok"] is True and "no JSON mode" in resp["message"] and resp["model"] == "q"
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert "response_format" in first and "response_format" not in second
    assert route.calls[1].request.headers["authorization"] == f"Bearer {LLM_KEY}"


async def test_test_llm_failure_is_redacted(client: httpx.AsyncClient) -> None:
    await client.put(
        "/api/settings", json={"llm": {"provider": "openai", "endpoint": LAN, "api_key": LLM_KEY}}
    )
    with respx.mock(assert_all_mocked=True) as router:
        router.post(LAN_CHAT).mock(return_value=httpx.Response(401, text=f"bad key {LLM_KEY}"))
        resp = (await client.post("/api/settings/test-llm")).json()
        assert resp["ok"] is False and "rejected the API key" in resp["message"]
        router.post(LAN_CHAT).mock(side_effect=httpx.ConnectError(f"boom {LLM_KEY}"))
        resp2 = await client.post("/api/settings/test-llm")
    assert resp2.status_code == 200 and resp2.json()["ok"] is False
    no_secrets(json.dumps(resp) + resp2.text)


def test_check_llm_never_raises() -> None:
    with respx.mock(assert_all_mocked=True) as router:
        router.post(LAN_CHAT).mock(return_value=httpx.Response(200, text="not json"))
        ok, message, model = check_llm(provider="openai", api_key="", endpoint=LAN, model="m")
    assert (ok, model) == (False, "m") and "unreadable" in message


# ------------------------------------------------------------------------------ LLM callers
def test_corrector_openai_falls_back_once() -> None:
    segments = [
        Segment(id=f"s{i}", speaker_id="A", start=i, end=i + 1, text="你好") for i in range(25)
    ]
    with respx.mock(assert_all_mocked=True) as router, httpx.Client() as http:
        route = router.post(LAN_CHAT).mock(
            side_effect=[
                httpx.Response(400, text="unsupported parameter: response_format"),
                chat_ok({"segments": []}),
                chat_ok('```\n{"segments": []}\n```'),
            ]
        )
        corrector = LlmCorrector(provider="openai", api_key="", endpoint=LAN, model="m", http=http)
        assert corrector.suggest(segments) == ([], 0)
    # batch 1: rejected + retried; batch 2: straight without response_format
    assert len(route.calls) == 3
    assert "response_format" not in json.loads(route.calls[2].request.content)
    assert all("authorization" not in c.request.headers for c in route.calls)


def test_dashscope_does_not_retry_without_json_mode() -> None:
    with respx.mock(assert_all_mocked=True) as router, httpx.Client() as http:
        route = router.post(DS_CHAT).mock(
            return_value=httpx.Response(400, text="bad response_format")
        )
        corrector = LlmCorrector(api_key="sk-x-0000000000", endpoint=DS, http=http)
        with pytest.raises(ProviderError, match="HTTP 400"):
            corrector.suggest([Segment(id="s", speaker_id="A", start=0, end=1, text="你好")])
    assert len(route.calls) == 1


def test_summarizer_openai() -> None:
    content = {"overview": "概述", "key_points": ["一"], "decisions": [], "action_items": []}
    with respx.mock(assert_all_mocked=True) as router, httpx.Client() as http:
        route = router.post(LAN_CHAT).mock(return_value=chat_ok(content))
        summary = Summarizer(
            provider="openai", api_key=LLM_KEY, endpoint=LAN, model="local", http=http
        ).summarize(
            [Segment(id="s", speaker_id="A", start=0, end=1, text="我们讨论了预算。")],
            [Speaker(id="A", name="甲", color="#1f77b4")],
        )
    assert summary.overview == "概述" and summary.model == "local"
    assert route.calls[0].request.headers["authorization"] == f"Bearer {LLM_KEY}"
