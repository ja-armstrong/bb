import pytest

from bitbeard.config import Config, load_config


def _set_env(monkeypatch, **overrides):
    values = {
        "QBITTORRENT_USERNAME": "admin",
        "QBITTORRENT_PASSWORD": "hunter2",
        "BASE_DOWNLOAD_DIR": "/media/downloads",
        "DISCORD_ANNOUNCE_CHANNEL_ID": "123456789",
        "DISCORD_BOT_TOKEN": "discord-token",
        "JACKETT_API_KEY": "jackett-key",
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
    assert config.discord_bot_token == "discord-token"
    assert config.jackett_api_key == "jackett-key"


def test_announce_channel_id_is_cast_to_int(monkeypatch):
    _set_env(monkeypatch, DISCORD_ANNOUNCE_CHANNEL_ID="987654321")

    config = load_config()

    assert config.announce_channel_id == 987654321
    assert isinstance(config.announce_channel_id, int)


@pytest.mark.parametrize(
    "missing",
    [
        "QBITTORRENT_USERNAME",
        "QBITTORRENT_PASSWORD",
        "BASE_DOWNLOAD_DIR",
        "DISCORD_ANNOUNCE_CHANNEL_ID",
        "DISCORD_BOT_TOKEN",
        "JACKETT_API_KEY",
    ],
)
def test_missing_required_var_raises_runtime_error_naming_it(monkeypatch, missing):
    _set_env(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        load_config()


def test_non_integer_announce_channel_id_raises(monkeypatch):
    _set_env(monkeypatch, DISCORD_ANNOUNCE_CHANNEL_ID="not-a-number")

    with pytest.raises(ValueError):
        load_config()
