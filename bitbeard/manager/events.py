"""Lifecycle Event emission for the Download Manager.

The manager emits Lifecycle Events (submitted/completed/drained) about the
torrents it owns. Every event goes to the API's ``POST /internal/events``
for the Audit Ledger, and Completion Events are mirrored to the Bot's
webhook for the Discord announcement (ADR-0001). Delivery is best-effort:
a sink that is down is logged and skipped, never allowed to break the
torrent lifecycle that is the manager's real job.

``transport`` is the seam for tests: a callable performing one HTTP POST.
Tests inject a recorder; production uses a thin ``requests`` adapter.
"""

import logging
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

#: Webhooks are tiny and local (shared network namespace); 5s is generous.
WEBHOOK_TIMEOUT = 5.0

#: Every event lands here; the API writes it to the Audit Ledger.
API_EVENTS_PATH = "/internal/events"

#: Completion Events (and only those) are mirrored to the Bot here.
BOT_WEBHOOK_PATH = "/internal/webhook/completion"


class Announcer(Protocol):
    """Seam for telling the outside world about Lifecycle Events."""

    async def announce_submitted(
        self,
        torrent_name: str,
        category: str,
        actor: str | None = None,
        download_id: int | None = None,
    ) -> None:
        """Announce that torrent_name (added under category) was submitted."""

    async def announce_completed(
        self,
        torrent_name: str,
        category: str,
        actor: str | None = None,
        download_id: int | None = None,
    ) -> None:
        """Announce that torrent_name (added under category) completed."""

    async def announce_all_done(self) -> None:
        """Announce that this manager has no downloads of its own left."""


def _default_transport(url: str, headers: dict, payload: dict) -> None:
    """Thin requests-based adapter over the transport seam."""
    requests.post(url, headers=headers, json=payload, timeout=WEBHOOK_TIMEOUT)


class WebhookAnnouncer:
    """Announcer that POSTs Lifecycle Events to the API and the Bot.

    Both sinks are optional at the call site (the Bot is the stack's one
    Optional Subsystem) but the API sink is where the Audit Ledger gets
    written, so a manager is built with it in production.
    """

    def __init__(
        self,
        api_base_url: str,
        service_token: str,
        bot_base_url: str | None = None,
        transport=None,
    ):
        self._api_events_url = api_base_url.rstrip("/") + API_EVENTS_PATH
        self._bot_webhook_url = (
            bot_base_url.rstrip("/") + BOT_WEBHOOK_PATH
            if bot_base_url
            else None
        )
        self._headers = {"Authorization": f"Bearer {service_token}"}
        self._transport = transport or _default_transport

    async def announce_submitted(
        self,
        torrent_name: str,
        category: str,
        actor: str | None = None,
        download_id: int | None = None,
    ) -> None:
        await self._emit("submitted", torrent_name, category, actor, download_id)

    async def announce_completed(
        self,
        torrent_name: str,
        category: str,
        actor: str | None = None,
        download_id: int | None = None,
    ) -> None:
        await self._emit(
            "completed",
            torrent_name,
            category,
            actor,
            download_id,
            mirror_to_bot=True,
        )

    async def announce_all_done(self) -> None:
        await self._emit("drained", None, None, None, None)

    async def _emit(
        self,
        event: str,
        torrent_name: str | None,
        category: str | None,
        actor: str | None,
        download_id: int | None,
        mirror_to_bot: bool = False,
    ) -> None:
        """Send one event to each sink, failing soft per sink."""
        payload = {
            "event": event,
            "torrent_name": torrent_name,
            "category": category,
            "actor": actor,
            "download_id": download_id,
        }
        self._post(self._api_events_url, payload)
        if mirror_to_bot and self._bot_webhook_url is not None:
            self._post(self._bot_webhook_url, payload)

    def _post(self, url: str, payload: dict) -> None:
        """Deliver one webhook or log the failure; never raise.

        The lifecycle must not depend on anyone listening: a dead API or
        Bot means a missed announcement, not a broken download.
        """
        try:
            self._transport(url, self._headers, payload)
        except Exception as exc:
            logger.warning(
                "Lifecycle Event %s (%s) not delivered to %s: %s",
                payload["event"],
                payload["torrent_name"],
                url,
                exc,
            )
