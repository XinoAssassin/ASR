"""``asr`` CLI: transcribe via the cloud provider (respx), probe, version, local error."""

from __future__ import annotations

import json

import pytest
import respx
from typer.testing import CliRunner

from asr import __version__
from asr.cli import app, parse_term
from tests.fixtures.dashscope.support import (
    API_KEY,
    ENDPOINT,
    fake_probe,
    make_audio,
    mock_dashscope,
    patch_audio,
    submitted_body,
)

runner = CliRunner()


@pytest.fixture
def cloud_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", API_KEY)
    monkeypatch.setenv("DASHSCOPE_ENDPOINT", ENDPOINT)
    monkeypatch.delenv("ASR_CLOUD_MODEL", raising=False)
    # No real sleeping between polls.
    from asr.providers.cloud import client

    monkeypatch.setattr(client, "sleep", lambda s: None)


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_parse_term():
    assert parse_term("Kubernetes:5").weight == 5
    assert parse_term("Qwen3-ASR:50").weight == 50
    t = parse_term("C++")
    assert (t.term, t.weight) == ("C++", 3)
    assert parse_term("a:b").term == "a:b"


def test_transcribe_cloud_txt_and_json(cloud_env, tmp_path, monkeypatch):
    patch_audio(monkeypatch, probe=fake_probe())
    audio = make_audio(tmp_path)
    with respx.mock(assert_all_called=False) as router:
        routes = mock_dashscope(router)
        result = runner.invoke(
            app,
            [
                "transcribe",
                str(audio),
                "--provider",
                "cloud",
                "--num-speakers",
                "4",
                "--terms",
                "Kubernetes:5",
                "--terms",
                "Vue 3:50",
            ],
        )
        assert result.exit_code == 0, result.output
        params = submitted_body(routes)["parameters"]
        assert params["vocabulary"] == {"Kubernetes": 5, "Vue 3": 50}
        assert params["speaker_count"] == 4
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 5
    assert lines[0].startswith("[00:00:00.1 - 00:00:03.8] SPEAKER_00: Hello world")
    assert API_KEY not in result.output

    out = tmp_path / "t.json"
    with respx.mock(assert_all_called=False) as router:
        mock_dashscope(router)
        result = runner.invoke(app, ["transcribe", str(audio), "-f", "json", "-o", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["provider"] == "cloud" and len(data["speakers"]) == 4


def test_transcribe_local_needs_models(tmp_path, monkeypatch):
    monkeypatch.setenv("ASR_MODEL_CACHE_DIR", str(tmp_path / "models"))
    audio = make_audio(tmp_path)
    result = runner.invoke(
        app, ["transcribe", str(audio), "--provider", "local", "--asr-model", "qwen3-asr-0.6b"]
    )
    assert result.exit_code == 2
    assert "asr models download" in result.output and "qwen3-asr-0.6b" in result.output
    url = runner.invoke(app, ["transcribe", "https://example.com/a.mp3", "--provider", "local"])
    assert url.exit_code == 2 and "not a URL" in url.output


def test_transcribe_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    result = runner.invoke(app, ["transcribe", str(make_audio(tmp_path))])
    assert result.exit_code == 2
    assert "DASHSCOPE_API_KEY" in result.output


def test_transcribe_provider_error_exit_code(cloud_env, tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_ENDPOINT", "https://evil.example.com")
    result = runner.invoke(app, ["transcribe", str(make_audio(tmp_path))])
    assert result.exit_code == 1
    assert "allow-list" in result.output
    assert API_KEY not in result.output


def test_probe_prints_json(tmp_path, monkeypatch):
    from asr.audio import decode

    monkeypatch.setattr(decode, "probe", lambda path, stream_index=0: fake_probe(channels=2))
    result = runner.invoke(app, ["probe", str(make_audio(tmp_path))])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["channels"] == 2
