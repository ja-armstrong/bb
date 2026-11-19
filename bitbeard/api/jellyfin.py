"""The API's one seam to the Jellyfin server's REST API (ticket #31).

Optional and off by default: the integration exists only when
``JELLYFIN_URL`` + ``JELLYFIN_API_TOKEN`` are set in the environment (the
wizard, ``.env.example`` and the compose api service carry them; the api's
``config`` owns the enabled decision). The only call the stack makes into
Jellyfin is provisioning a user on registration approval — the same
username as the approved Account, with a random password generated here
and never displayed, stored or logged. The user self-serves a new one
later from the Webui (ticket #32): ``JellyfinClient.set_password``
overwrites the generated password on the matching Jellyfin user, through
the same admin API key — which needs no current password, so the random
one is involved nowhere.

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


class JellyfinUserMissingError(JellyfinProvisioningError):
    """No Jellyfin user matches the requested username.

    A subclass, not a message-parsing convention: the self-service
    password endpoint (ticket #32) refuses differently on this case —
    the caller is logged in, so naming the absence is fine — while every
    other failure stays one unreachable/refused hop.
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


#: Jellyfin 12.x requires the provider ids inline on user creation; these
#: are the stock providers every default install has (read back from the
#: server's own users, so a custom-provider install would need an edit).
DEFAULT_AUTH_PROVIDER = "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
DEFAULT_RESET_PROVIDER = "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider"


class JellyfinClient:
    """The Jellyfin REST calls provisioning needs, and nothing more.

    Auth is the API token in the ``Authorization: MediaBrowser Token="…"``
    header (the admin token from the Dashboard -> API Keys page). Recent
    Jellyfin releases reject a bare API key in ``X-Emby-Token`` with 401
    (observed on 12.x), while the Authorization form works across
    versions; the ``api_key`` query parameter is rejected there too and
    would leak the key into URLs anyway. Endpoints, per the ticket:

    - ``GET /Users`` — the username-taken check before creating.
    - ``POST /Users/New`` with ``{"Name": username}`` — creates the user
      (Jellyfin 12.x moved creation off ``POST /Users``: that route now
      dispatches to the update action and fails with an empty Guid, and
      it additionally demands the auth/password-reset provider ids in
      the body — both pinned here).
    - ``POST /Users/{Id}/Password`` with ``{"Id": ..., "NewPw": ...}`` —
      sets the fresh user's password (a just-created user has none, so no
      current password rides along).

    ``set_password`` (ticket #32) reuses the lookup and the same
    password endpoint: the admin key may set any user's password without
    presenting the current one, which is exactly what the self-service
    flow leans on — the generated provisioning password is overwritten
    and never asked for or revealed.
    """

    def __init__(self, base_url: str, api_token: str, transport=None):
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._transport = transport or _default_transport

    def _headers(self) -> dict:
        return {
            "Authorization": f'MediaBrowser Token="{self._token}"'
        }

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
            f"{self._base_url}/Users/New",
            headers=self._headers(),
            json={
                "Name": username,
                # Jellyfin 12.x answers 400 without these two: the policy
                # must name its auth and password-reset providers (the
                # stock pair; a custom-provider install edits the consts).
                "Policy": {
                    "AuthenticationProviderId": DEFAULT_AUTH_PROVIDER,
                    "PasswordResetProviderId": DEFAULT_RESET_PROVIDER,
                },
            },
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

    def set_password(self, username: str, password: str) -> None:
        """Set the password of the Jellyfin user matching ``username``.

        The self-service half of provisioning (ticket #32): the caller is
        the Account owner, so the user is looked up by the same
        case-insensitive name match ``create_user`` checks before
        creating. The admin key needs no current password, so the
        generated one from provisioning is simply overwritten — it was
        never shown to anyone and is involved nowhere here.

        Raises ``JellyfinUserMissingError`` when no Jellyfin user
        matches (the endpoint turns that into its own plain refusal),
        ``JellyfinProvisioningError`` for every other failure the caller
        is meant to survive: an unreachable server, a rejected token, or
        Jellyfin refusing the set.
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
        user_id = None
        if isinstance(users, list):
            for user in users:
                if (
                    isinstance(user, dict)
                    and str(user.get("Name", "")).lower() == username.lower()
                ):
                    user_id = user.get("Id")
                    break
        if not user_id:
            raise JellyfinUserMissingError(
                f"no Jellyfin user named '{username}' exists"
            )

        status, _ = self._transport(
            "POST",
            f"{self._base_url}/Users/{user_id}/Password",
            headers=self._headers(),
            json={"Id": user_id, "NewPw": password},
        )
        if status == 401:
            raise JellyfinProvisioningError(
                "Jellyfin rejected the API token (401 unauthorized)"
            )
        if status not in (200, 204):
            raise JellyfinProvisioningError(
                f"Jellyfin refused to set the user's password (HTTP {status})"
            )
