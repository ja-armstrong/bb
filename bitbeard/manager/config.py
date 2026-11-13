"""Download Manager configuration, read once from the environment."""

from dataclasses import dataclass

from shared.config import env

#: All services share gluetun's network namespace, so localhost is always
#: the right host for the internal hops.
DEFAULT_API_URL = "http://127.0.0.1:9870"
DEFAULT_BOT_URL = "http://127.0.0.1:9872"


@dataclass(frozen=True)
class Config:
    qbit_username: str
    qbit_password: str
    base_download_dir: str
    jackett_api_key: str
    service_token: str
    api_url: str = DEFAULT_API_URL
    bot_url: str = DEFAULT_BOT_URL


def load_config() -> Config:
    return Config(
        qbit_username=env("QBITTORRENT_USERNAME"),
        qbit_password=env("QBITTORRENT_PASSWORD"),
        base_download_dir=env("BASE_DOWNLOAD_DIR"),
        jackett_api_key=env("JACKETT_API_KEY"),
        service_token=env("SERVICE_TOKEN"),
        api_url=env("API_URL", default=DEFAULT_API_URL),
        bot_url=env("BOT_URL", default=DEFAULT_BOT_URL),
    )
