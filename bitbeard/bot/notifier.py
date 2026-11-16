"""The Bot's Completion Event announcement.

The Download Manager mirrors Completion Events to the Bot's webhook; this
module owns the last hop: posting the announcement to the configured
Discord channel. The Discord client is injected, so tests drive the webhook
endpoint with a stub client and never touch Discord.
"""

import logging

logger = logging.getLogger(__name__)


class CompletionNotifier:
    """Posts a Discord message when a Completion Event arrives."""

    def __init__(self, client, channel_id: int | None):
        self._client = client
        self._channel_id = channel_id

    @property
    def available(self) -> bool:
        """True when a connected client has a channel configured."""
        return self._channel_id is not None and not self._client.is_closed()

    async def announce(self, torrent_name: str, category: str | None) -> None:
        """Post the announcement for one completed Download."""
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            logger.warning(
                "Announcement channel %s not found; skipping announcement.",
                self._channel_id,
            )
            return
        message = (
            # USER-FACING
            f"{torrent_name} has been added to Bitbeard under {category}."
        )
        await channel.send(message)


class NullNotifier:
    """Stand-in used when Discord is not configured.

    The Bot stays up without credentials so the Optional Subsystem can run
    before Discord exists: Completion Events are accepted and dropped.
    """

    @property
    def available(self) -> bool:
        return False

    async def announce(self, torrent_name: str, category: str | None) -> None:
        logger.info(
            "Discord unconfigured; dropped announcement for %r.",
            torrent_name,
        )
