"""Tests for Download Manager configuration."""
import pytest

from manager.config import Config, load_config


def _set_env(monkeypatch, **overrides):
    values = {
        "QBITTORRENT_USERNAME": "admin",
        "QBITTORRENT_PASSWORD": "hunter2",
        "BASE_DOWNLOAD_DIR": "/media/downloads",
        "JACKETT_API_KEY": "jackett-key",
        "SERVICE_TOKEN": "service-token",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_load_config_reads_all_env_vars(monkeypatch):
    _set_env(monkeypatch)

    config = load_config()

    assert isinstance(config, Config)
    assert config.qbit_username == "admin"
    assert config.qbit_password == "hunter2"
    assert config.base_download_dir == "/media/downloads"
    assert config.jackett_api_key == "jackett-key"
    assert config.service_token == "service-token"


def test_internal_hop_urls_have_defaults(monkeypatch):
    """All services share gluetun's netns, so the defaults are localhost."""
    _set_env(monkeypatch)
    monkeypatch.delenv("API_URL", raising=False)
    monkeypatch.delenv("BOT_URL", raising=False)

    config = load_config()

    assert config.api_url == "http://127.0.0.1:9870"
    assert config.bot_url == "http://127.0.0.1:9872"


def test_internal_hop_urls_are_configurable(monkeypatch):
    _set_env(monkeypatch, API_URL="http://api:9870", BOT_URL="http://bot:9872")

    config = load_config()

    assert config.api_url == "http://api:9870"
    assert config.bot_url == "http://bot:9872"


@pytest.mark.parametrize(
    "missing",
    [
        "QBITTORRENT_USERNAME",
        "QBITTORRENT_PASSWORD",
        "BASE_DOWNLOAD_DIR",
        "JACKETT_API_KEY",
        "SERVICE_TOKEN",
    ],
)
def test_missing_required_var_raises_runtime_error_naming_it(monkeypatch, missing):
    _set_env(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        load_config()
