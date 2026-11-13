import logging
import os
from dataclasses import dataclass


@dataclass
class Config:
    qbit_username: str
    qbit_password: str
    base_download_dir: str
    announce_channel_id: int
    discord_bot_token: str
    jackett_api_key: str


def _get_env_var(name, cast=None):
    value = os.environ.get(name)
    if value is None:
        logging.error(f"Required environment variable '{name}' not set.")
        raise RuntimeError(f"Required environment variable '{name}' not set.")
    if cast is not None:
        try:
            value = cast(value)
        except Exception as e:
            logging.error(f"Failed to cast environment variable '{name}': {e}")
            raise
    return value


def load_config():
    """Read all configuration from environment variables.

    Called once at startup; callers pass the returned values around
    instead of re-reading the environment.
    """
    return Config(
        qbit_username=_get_env_var('QBITTORRENT_USERNAME'),
        qbit_password=_get_env_var('QBITTORRENT_PASSWORD'),
        base_download_dir=_get_env_var('BASE_DOWNLOAD_DIR'),
        announce_channel_id=_get_env_var('DISCORD_ANNOUNCE_CHANNEL_ID', cast=int),
        discord_bot_token=_get_env_var('DISCORD_BOT_TOKEN'),
        jackett_api_key=_get_env_var('JACKETT_API_KEY'),
    )
