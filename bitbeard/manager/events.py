"""Lifecycle Event emission for the Download Manager.

The manager emits events about torrents it owns (submitted/completed/drained).
In this scaffold the sink is logging; the API and Bot webhook sinks are wired
in the follow-up tickets that own those endpoints.
"""

import logging


class LoggingAnnouncer:
    """Announcer that records Lifecycle Events in the service log."""

    async def announce_completed(self, torrent_name: str, category: str) -> None:
        logging.info(
            "%s has been added to Bitbeard under %s.", torrent_name, category
        )

    async def announce_all_done(self) -> None:
        logging.info("All downloads completed.")
