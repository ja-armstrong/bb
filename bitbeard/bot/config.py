"""The Bitbeard Bot service: the Discord Frontend."""

from dataclasses import dataclass

from shared.config import env

DEFAULT_API_URL = "http://127.0.0.1:9870"


@dataclass(frozen=True)
class Config:
    discord_bot_token: str | None
    announce_channel_id: int | None
    api_url: str
    service_token: str


def load_config() -> Config:
    return Config(
        discord_bot_token=env("DISCORD_BOT_TOKEN", default=None),
        announce_channel_id=env(
            "DISCORD_ANNOUNCE_CHANNEL_ID", cast=int, default=None
        ),
        api_url=env("API_URL", default=DEFAULT_API_URL),
        service_token=env("SERVICE_TOKEN"),
    )
