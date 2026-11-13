"""Download Manager configuration, read once from the environment."""

from dataclasses import dataclass

from shared.config import env


@dataclass(frozen=True)
class Config:
    qbit_username: str
    qbit_password: str
    base_download_dir: str
    jackett_api_key: str
    service_token: str


def load_config() -> Config:
    return Config(
        qbit_username=env("QBITTORRENT_USERNAME"),
        qbit_password=env("QBITTORRENT_PASSWORD"),
        base_download_dir=env("BASE_DOWNLOAD_DIR"),
        jackett_api_key=env("JACKETT_API_KEY"),
        service_token=env("SERVICE_TOKEN"),
    )
