"""The API's single seam to the Download Manager (ADR-0001, ADR-0002).

The API owns no media logic: Search, the torrent lifecycle and progress all
live in the Download Manager. This module is the one place the API reaches
it from, forwarding the shared ``SERVICE_TOKEN`` and the acting Account as
trusted headers so actions stay attributable end to end. No qBittorrent or
Jackett code may ever live behind this seam.

``transport`` is the seam for tests: a callable performing one HTTP request
and returning a ``ProxyResponse``. The default adapter imports ``requests``
lazily, so importing ``api.main`` stays free of the dependency — the
import-safety test pins that.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Proxied calls are small JSON round-trips on a shared network namespace.
DEFAULT_TIMEOUT = 10.0

#: Trusted headers (ADR-0002): the acting Account on the proxied request.
ACCOUNT_HEADER = "X-Bitbeard-Account"
DISCORD_HEADER = "X-Bitbeard-Discord"


class ManagerUnavailable(Exception):
    """The Download Manager could not be reached or answered unusably."""


@dataclass(frozen=True)
class ProxyResponse:
    """The manager's reply, shaped without any HTTP session state."""

    status_code: int
    payload: Any


Transport = Callable[..., ProxyResponse]


def _default_transport(
    method: str, url: str, *, headers: dict, params: dict | None, json: Any
) -> ProxyResponse:
    """One blocking HTTP round-trip, errors mapped to ``ManagerUnavailable``."""
    import requests  # lazy: keeps `import api.main` dependency-free

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json,
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ManagerUnavailable(str(exc)) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return ProxyResponse(response.status_code, payload)


class ManagerProxy:
    """Forwards API requests to the Download Manager with service auth.

    Every call carries the ``SERVICE_TOKEN`` bearer; when the acting
    Account is known, its name (and linked Discord name) ride along as
    trusted headers so the manager — and the events it emits — can name
    the Actor.
    """

    def __init__(
        self, base_url: str, service_token: str, transport: Transport | None = None
    ):
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._transport = transport or _default_transport

    def forward(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        account: str | None = None,
        discord: str | None = None,
    ) -> ProxyResponse:
        """Send one request to the manager or raise ``ManagerUnavailable``."""
        headers = {"Authorization": f"Bearer {self._service_token}"}
        if account is not None:
            headers[ACCOUNT_HEADER] = account
        if discord is not None:
            headers[DISCORD_HEADER] = discord
        try:
            return self._transport(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                params=params,
                json=json,
            )
        except ManagerUnavailable:
            raise
        except Exception as exc:  # a broken transport is still "unavailable"
            raise ManagerUnavailable(str(exc)) from exc
