"""API configuration, read once from the environment at startup."""

from dataclasses import dataclass

from shared.config import env

DEFAULT_DB_PATH = "/data/bitbeard.db"
DEFAULT_QUOTA_GB = 50.0
DEFAULT_SESSION_TTL_HOURS = 24.0
DEFAULT_MANAGER_URL = "http://127.0.0.1:9871"
# The docker-compose project name: the label value the docker-control
# listing filters on (ADR-0003). docker-compose derives the same name from
# the directory unless COMPOSE_PROJECT_NAME says otherwise.
DEFAULT_COMPOSE_PROJECT = "bitbeard"
# Where the compose file mounts the host's ./openvpn — the VPN settings
# the Admin Panel writes live there (api.vpn).
DEFAULT_OPENVPN_DIR = "/openvpn"
# The stack is served behind the HTTPS reverse proxy by default, so
# login Session cookies carry the Secure flag. PUBLIC_HTTPS=0 is the
# plain-HTTP escape hatch — see docs/proxy.md for the footgun.
DEFAULT_PUBLIC_HTTPS = True
# The Jellyfin server's default URL (ticket #31): how the api container
# reaches the Jellyfin Subsystem's host-published 8096, through the host
# gateway the compose api service carries as an extra_host. The wizard
# and .env.example document the same value; this constant is the Python
# side's single home for it.
DEFAULT_JELLYFIN_URL = "http://host.docker.internal:8096"
GIB = 1024 ** 3


def _env_bool(value: str) -> bool:
    """A tolerant boolean cast: 1/true/yes/on, 0/false/no/off."""
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


@dataclass(frozen=True)
class Config:
    db_path: str
    service_token: str
    admin_user: str
    admin_password: str
    default_quota_gb: float
    session_ttl_hours: float = DEFAULT_SESSION_TTL_HOURS
    manager_url: str = DEFAULT_MANAGER_URL
    compose_project: str = DEFAULT_COMPOSE_PROJECT
    openvpn_dir: str = DEFAULT_OPENVPN_DIR
    #: The VPN provider the compose .env seeded; the Admin Panel's written
    #: settings (openvpn/vpn.env) take precedence once they exist.
    vpn_provider: str = ""
    #: True (the default) while the stack is served behind the HTTPS
    #: reverse proxy; Session cookies then carry the Secure flag.
    public_https: bool = DEFAULT_PUBLIC_HTTPS
    #: The optional Jellyfin provisioning integration (ticket #31): both
    #: keys together turn it on, the token alone decides — the URL always
    #: has a documented default.
    jellyfin_url: str = DEFAULT_JELLYFIN_URL
    jellyfin_api_token: str = ""

    @property
    def default_quota_bytes(self) -> int:
        """The global Storage Quota default, in bytes."""
        return int(self.default_quota_gb * GIB)

    @property
    def jellyfin_enabled(self) -> bool:
        """Whether the Jellyfin provisioning integration is on: both
        ``JELLYFIN_URL`` and ``JELLYFIN_API_TOKEN`` are set."""
        return bool(self.jellyfin_url and self.jellyfin_api_token)


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
        compose_project=env(
            "COMPOSE_PROJECT_NAME", default=DEFAULT_COMPOSE_PROJECT
        ),
        openvpn_dir=env("OPENVPN_DIR", default=DEFAULT_OPENVPN_DIR),
        vpn_provider=env("VPN_PROVIDER", default=""),
        public_https=env(
            "PUBLIC_HTTPS", cast=_env_bool, default=DEFAULT_PUBLIC_HTTPS
        ),
        jellyfin_url=env("JELLYFIN_URL", default=DEFAULT_JELLYFIN_URL),
        jellyfin_api_token=env("JELLYFIN_API_TOKEN", default=""),
    )
