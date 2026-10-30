"""Summary / L3 suggestion routes with the OpenAI-compatible LLM provider (respx, no network)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import FastAPI

from server.config import EnvSettings
from tests.conftest import EnvFactory
from tests.test_editing import _transcript
from tests.test_online_settings import (
    ENV_KEY,
    LAN,
    LAN_CHAT,
    chat_ok,
    default_config,
    no_secrets,
    write_config,
)


@pytest.fixture
def env(env_factory: EnvFactory) -> EnvSettings:
    return env_factory(DASHSCOPE_API_KEY=ENV_KEY)  # cloud jobs need it; it must not reach LAN


async def test_routes_use_the_openai_provider(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    write_config(
        default_config(env),
        f'[llm]\nprovider = "openai"\nendpoint = "{LAN}"\nmodel = "qwen3-8b"\n',
    )
    url = f"/api/transcripts/{t['id']}"
    summary = {"overview": "概述", "key_points": ["一"], "decisions": [], "action_items": []}
    with respx.mock(assert_all_mocked=False) as router:
        route = router.post(LAN_CHAT).mock(
            side_effect=[
                chat_ok({"segments": [{"id": "seg_00000", "text": "Townfall真好玩，我们开始啦"}]}),
                chat_ok(summary),
            ]
        )
        sugg = await client.post(f"{url}/llm-suggestions", json={})
        summ = await client.post(f"{url}/summary")
    assert sugg.status_code == 200, sugg.text
    assert sugg.json()["added"] == 1
    assert summ.status_code == 200, summ.text
    assert summ.json()["model"] == "qwen3-8b"
    for call in route.calls:
        assert "authorization" not in call.request.headers  # the DashScope key stays home
        assert json.loads(call.request.content)["model"] == "qwen3-8b"
    no_secrets(sugg.text + summ.text)


async def test_routes_openai_without_endpoint_is_422(
    app: FastAPI, client: httpx.AsyncClient, env: EnvSettings
) -> None:
    t = await _transcript(app, client, env)
    await client.put("/api/settings", json={"llm": {"provider": "openai"}})
    resp = await client.post(f"/api/transcripts/{t['id']}/summary")
    assert resp.status_code == 422 and "not configured" in resp.json()["detail"]
