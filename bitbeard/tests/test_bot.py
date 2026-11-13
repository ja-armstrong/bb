"""Tests for the Bot service config and its scaffold HTTP listener."""
import pytest
from fastapi.testclient import TestClient

from bot.config import Config, load_config
from bot.main import create_app


def _set_env(monkeypatch, **overrides):
    values = {
        "DISCORD_BOT_TOKEN": "discord-token",
        "DISCORD_ANNOUNCE_CHANNEL_ID": "123456789",
        "API_URL": "http://127.0.0.1:9870",
        "SERVICE_TOKEN": "service-token",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_load_config_reads_all_env_vars(monkeypatch):
    _set_env(monkeypatch)

    config = load_config()

    assert isinstance(config, Config)
    assert config.discord_bot_token == "discord-token"
    assert config.announce_channel_id == 123456789
    assert config.api_url == "http://127.0.0.1:9870"
    assert config.service_token == "service-token"


def test_discord_token_is_optional(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    monkeypatch.delenv("DISCORD_ANNOUNCE_CHANNEL_ID")

    config = load_config()

    assert config.discord_bot_token is None
    assert config.announce_channel_id is None


def test_api_url_has_a_default(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("API_URL")

    config = load_config()

    assert config.api_url == "http://127.0.0.1:9870"


def test_service_token_is_required(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("SERVICE_TOKEN")

    with pytest.raises(RuntimeError, match="SERVICE_TOKEN"):
        load_config()


def test_non_integer_announce_channel_id_raises(monkeypatch):
    _set_env(monkeypatch, DISCORD_ANNOUNCE_CHANNEL_ID="not-a-number")

    with pytest.raises(ValueError):
        load_config()


def test_health_endpoint_works_without_a_discord_token():
    config = Config(
        discord_bot_token=None,
        announce_channel_id=None,
        api_url="http://127.0.0.1:9870",
        service_token="service-token",
    )

    with TestClient(create_app(config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "bot"}
