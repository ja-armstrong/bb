"""API configuration, read once from the environment at startup."""

from dataclasses import dataclass

from shared.config import env

DEFAULT_DB_PATH = "/data/bitbeard.db"
DEFAULT_QUOTA_GB = 50.0
DEFAULT_SESSION_TTL_HOURS = 24.0
DEFAULT_MANAGER_URL = "http://127.0.0.1:9871"
GIB = 1024 ** 3


@dataclass(frozen=True)
class Config:
    db_path: str
    service_token: str
    admin_user: str
    admin_password: str
    default_quota_gb: float
    session_ttl_hours: float = DEFAULT_SESSION_TTL_HOURS
    manager_url: str = DEFAULT_MANAGER_URL

    @property
    def default_quota_bytes(self) -> int:
        """The global Storage Quota default, in bytes."""
        return int(self.default_quota_gb * GIB)


def load_config() -> Config:
    return Config(
        db_path=env("DATABASE_PATH", default=DEFAULT_DB_PATH),
        service_token=env("SERVICE_TOKEN"),
        admin_user=env("ADMIN_USER"),
        admin_password=env("ADMIN_PASSWORD"),
        default_quota_gb=env("DEFAULT_QUOTA_GB", cast=float, default=DEFAULT_QUOTA_GB),
        session_ttl_hours=env(
            "SESSION_TTL_HOURS", cast=float, default=DEFAULT_SESSION_TTL_HOURS
        ),
        manager_url=env("MANAGER_URL", default=DEFAULT_MANAGER_URL),
    )
