"""The API's one seam to the Jellyfin server's REST API (ticket #31).

Optional and off by default: the integration exists only when
``JELLYFIN_URL`` + ``JELLYFIN_API_TOKEN`` are set in the environment (the
wizard, ``.env.example`` and the compose api service carry them; the api's
``config`` owns the enabled decision). The only call the stack makes into
Jellyfin is provisioning a user on registration approval — the same
username as the approved Account, with a random password generated here
and never displayed, stored or logged. The user self-serves a new one
later (the next ticket's flow).

``transport`` is the seam for tests: a callable performing one HTTP
request and returning a ``(status, payload)`` tuple, mirroring the shape
``manager_proxy`` uses. The default adapter imports ``requests`` lazily,
so importing ``api.main`` stays free of the dependency — the import-safety
test pins that.
"""

import secrets
from typing import Any, Callable

#: Provisioning calls are small JSON round-trips on the shared network.
DEFAULT_TIMEOUT = 10.0


class JellyfinProvisioningError(Exception):
    """One provisioning failure, carrying the reason the Admin is shown.

    The reason never contains the generated password: it is built from
    HTTP statuses and the requested username only.
    """


#: The transport's answer: ``(http_status, json_payload_or_None)``.
Transport = Callable[..., "tuple[int, Any]"]


def _default_transport(
    method: str, url: str, *, headers: dict, json: Any
) -> "tuple[int, Any]":
    """One blocking HTTP round-trip, unreachable mapped to the failure."""
    import requests  # lazy: keeps `import api.main` dependency-free

    try:
        response = requests.request(
            method, url, headers=headers, json=json, timeout=DEFAULT_TIMEOUT
        )
    except requests.RequestException as exc:
        raise JellyfinProvisioningError(
            f"Jellyfin could not be reached ({exc})"
        ) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def generate_password() -> str:
    """A crypto-random password for the provisioned Jellyfin user.

    Never displayed, logged or ledgared — nobody in the stack sees it;
    the user sets their own from the Webui later.
    """
    return secrets.token_urlsafe(18)


class JellyfinClient:
    """The Jellyfin REST calls provisioning needs, and nothing more.

    Auth is the API token in the ``X-Emby-Token`` header (the admin token
    from the Dashboard -> API Keys page). Endpoints, per the ticket:

    - ``GET /Users`` — the username-taken check before creating.
    - ``POST /Users`` with ``{"Name": username}`` — creates the user.
    - ``POST /Users/{Id}/Password`` with ``{"Id": ..., "NewPw": ...}`` —
      sets the fresh user's password (a just-created user has none, so no
      current password rides along).
    """

    def __init__(self, base_url: str, api_token: str, transport=None):
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._transport = transport or _default_transport

    def _headers(self) -> dict:
        return {"X-Emby-Token": self._token}

    def create_user(self, username: str, password: str) -> None:
        """Create the Jellyfin user ``username`` with ``password``.

        Raises ``JellyfinProvisioningError`` on every failure the caller
        is meant to survive: an unreachable server, a rejected token, a
        username already taken, or Jellyfin refusing a step.
        """
        status, users = self._transport(
            "GET", f"{self._base_url}/Users", headers=self._headers(), json=None
        )
        if status == 401:
            raise JellyfinProvisioningError(
                "Jellyfin rejected the API token (401 unauthorized)"
            )
        if status != 200:
            raise JellyfinProvisioningError(
                f"Jellyfin answered {status} instead of 200 for its user list"
            )
        if isinstance(users, list):
            for user in users:
                if (
                    isinstance(user, dict)
                    and str(user.get("Name", "")).lower() == username.lower()
                ):
                    raise JellyfinProvisioningError(
                        f"a Jellyfin user named '{username}' already exists"
                    )

        status, payload = self._transport(
            "POST",
            f"{self._base_url}/Users",
            headers=self._headers(),
            json={"Name": username},
        )
        if status == 401:
            raise JellyfinProvisioningError(
                "Jellyfin rejected the API token (401 unauthorized)"
            )
        if status not in (200, 201) or not isinstance(payload, dict):
            raise JellyfinProvisioningError(
                f"Jellyfin refused to create the user (HTTP {status})"
            )
        user_id = payload.get("Id")
        if not user_id:
            raise JellyfinProvisioningError(
                "Jellyfin did not name the created user's id"
            )

        status, _ = self._transport(
            "POST",
            f"{self._base_url}/Users/{user_id}/Password",
            headers=self._headers(),
            json={"Id": user_id, "CurrentPw": "", "NewPw": password},
        )
        if status == 401:
            raise JellyfinProvisioningError(
                "Jellyfin rejected the API token (401 unauthorized)"
            )
        if status not in (200, 204):
            raise JellyfinProvisioningError(
                f"Jellyfin refused to set the user's password (HTTP {status})"
            )
