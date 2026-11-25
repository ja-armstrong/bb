"""Download Manager configuration, read once from the environment."""

from dataclasses import dataclass

from shared.config import env

#: All services share gluetun's network namespace, so localhost is always
#: the right host for the internal hops.
DEFAULT_API_URL = "http://127.0.0.1:9870"
DEFAULT_BOT_URL = "http://127.0.0.1:9872"

#: The shared SQLite store (the same file the API owns) and how often the
#: Storage Quota reconciliation job runs. ``0`` disables the job.
DEFAULT_DB_PATH = "/data/bitbeard.db"
DEFAULT_RECONCILE_INTERVAL_SECONDS = 3600.0

#: How often the Stall Watchdog sweeps qBittorrent (``0`` disables the
#: job), and after how many consecutive stalled sweeps a torrent's
#: recovery escalates from a forced reannounce to rotating the listening
#: port (the wedged-UDP-socket cure).
DEFAULT_STALL_WATCH_INTERVAL_SECONDS = 300.0
DEFAULT_STALL_PORT_ROTATE_POLLS = 3


@dataclass(frozen=True)
class Config:
    qbit_username: str
    qbit_password: str
    base_download_dir: str
    jackett_api_key: str
    service_token: str
    api_url: str = DEFAULT_API_URL
    bot_url: str = DEFAULT_BOT_URL
    db_path: str = DEFAULT_DB_PATH
    reconcile_interval: float = DEFAULT_RECONCILE_INTERVAL_SECONDS
    stall_watch_interval: float = DEFAULT_STALL_WATCH_INTERVAL_SECONDS
    stall_port_rotate_polls: int = DEFAULT_STALL_PORT_ROTATE_POLLS


def load_config() -> Config:
    return Config(
        qbit_username=env("QBITTORRENT_USERNAME"),
        qbit_password=env("QBITTORRENT_PASSWORD"),
        base_download_dir=env("BASE_DOWNLOAD_DIR"),
        jackett_api_key=env("JACKETT_API_KEY"),
        service_token=env("SERVICE_TOKEN"),
        api_url=env("API_URL", default=DEFAULT_API_URL),
        bot_url=env("BOT_URL", default=DEFAULT_BOT_URL),
        db_path=env("DATABASE_PATH", default=DEFAULT_DB_PATH),
        reconcile_interval=env(
            "QUOTA_RECONCILE_INTERVAL",
            cast=float,
            default=DEFAULT_RECONCILE_INTERVAL_SECONDS,
        ),
        stall_watch_interval=env(
            "STALL_WATCH_INTERVAL",
            cast=float,
            default=DEFAULT_STALL_WATCH_INTERVAL_SECONDS,
        ),
        stall_port_rotate_polls=env(
            "STALL_PORT_ROTATE_POLLS",
            cast=int,
            default=DEFAULT_STALL_PORT_ROTATE_POLLS,
        ),
    )
