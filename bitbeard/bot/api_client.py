"""The Bot's one seam to the API (ADR-0002).

The Bot is a thin Frontend: it holds no privileged access of its own and
authenticates to the API as a Service Account with the shared
``SERVICE_TOKEN``. Every call carries the acting Discord user's ID (and
display name) in the trusted headers, so the API can attribute the action —
to the linked Account when there is one, or to ``discord:<name>`` in the
Audit Ledger when the Discord user is unlinked.

``transport`` is the seam for tests: a callable performing one HTTP request
and returning an ``ApiResult``. The default adapter imports ``requests``
lazily, so importing ``bot.main`` stays free of the dependency.
"""

from dataclasses import dataclass
from typing import Any, Callable

#: Trusted headers the API reads on bot-proxied calls (see api.main).
DISCORD_ID_HEADER = "X-Bitbeard-Discord"
DISCORD_NAME_HEADER = "X-Bitbeard-Discord-Name"

#: Bot->API calls are small JSON round-trips on a shared network namespace.
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class ApiResult:
    """The API's reply, shaped without any HTTP session state."""

    status_code: int
    payload: Any


Transport = Callable[..., ApiResult]


def _default_transport(
    method: str, url: str, *, headers: dict, params: dict | None, json: Any
) -> ApiResult:
    """One blocking HTTP round-trip; transport errors raise to ``_call``."""
    import requests  # lazy: keeps `import bot.main` dependency-free

    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json,
        timeout=DEFAULT_TIMEOUT,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return ApiResult(response.status_code, payload)


class ApiClient:
    """Calls the API as the Service Account, acting for a Discord user.

    Every method takes the acting Discord user's ID; the display name rides
    along when it is known so unlinked users are recorded as
    ``discord:<name>`` rather than as a bare Discord ID.
    """

    def __init__(
        self, base_url: str, service_token: str, transport: Transport | None = None
    ):
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._transport = transport or _default_transport

    def _headers(self, discord_id: str, discord_name: str | None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._service_token}",
            DISCORD_ID_HEADER: discord_id,
        }
        if discord_name is not None:
            headers[DISCORD_NAME_HEADER] = discord_name
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        discord_id: str,
        discord_name: str | None = None,
        params: dict | None = None,
        json: Any = None,
    ) -> ApiResult:
        return self._try(
            lambda: self._transport(
                method,
                f"{self._base_url}{path}",
                headers=self._headers(discord_id, discord_name),
                params=params,
                json=json,
            )
        )

    @staticmethod
    def _try(call) -> ApiResult:
        """Perform one call, surfacing transport failures as a 503 result.

        The Discord UI decides how to present a failure; the Bot never
        takes down its listener over an unreachable API.
        """
        try:
            return call()
        except Exception as exc:
            # USER-FACING
            return ApiResult(503, {"detail": f"API unreachable: {exc}"})

    def search(
        self, q: str, *, discord_id: str, discord_name: str | None = None
    ) -> ApiResult:
        """Search through the API, as the acting Discord user."""
        return self._call(
            "GET",
            "/search",
            params={"q": q},
            discord_id=discord_id,
            discord_name=discord_name,
        )

    def submit(
        self,
        magnet_link: str,
        category: str,
        *,
        discord_id: str,
        discord_name: str | None = None,
        size_bytes: int = 0,
        title: str | None = None,
    ) -> ApiResult:
        """Submit a Download through the API, as the acting Discord user.

        When the magnet came from a Search result, its size and title
        ride along: the API's Storage Quota pre-check and the manager's
        ownership record both need the real numbers.
        """
        return self._call(
            "POST",
            "/download",
            json={
                "magnet_link": magnet_link,
                "category": category,
                "size_bytes": size_bytes,
                "title": title,
            },
            discord_id=discord_id,
            discord_name=discord_name,
        )

    def media(
        self, *, discord_id: str, discord_name: str | None = None
    ) -> ApiResult:
        """The acting user's own Download records, through the API.

        This is the Bot's one source for both progress and cancel: the
        listing is scoped to the Actor the headers name, so the Bot only
        ever shows — and only offers to cancel — the user's own
        In-flight Downloads.
        """
        return self._call(
            "GET", "/media", discord_id=discord_id, discord_name=discord_name
        )

    def media_cancel(
        self, download_id: int, *, discord_id: str, discord_name: str | None = None
    ) -> ApiResult:
        """Cancel one In-flight Download the acting user owns.

        Download-level, via the API's ``/media/{id}/cancel``: the old
        cancel-everything behavior is intentionally gone (the spec), so
        each Download is cancelled explicitly, scoped to its owner.
        """
        return self._call(
            "POST",
            f"/media/{download_id}/cancel",
            discord_id=discord_id,
            discord_name=discord_name,
        )
