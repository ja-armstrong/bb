"""Deep Download module: the torrent lifecycle (add -> poll -> announce -> delete).

Callers learn a small interface — ``submit``, ``progress``, ``cancel_all``
and ``format_eta`` — and get the whole polling loop, hash-scoped completion
sweep, and announcement wiring behind it. The client, config, announcer and
poll interval are accepted as dependencies, never created here.
"""

import asyncio
import logging
import os
from typing import Protocol


class Announcer(Protocol):
    """Seam for telling the outside world about lifecycle events."""

    async def announce_completed(self, torrent_name: str, category: str) -> None:
        """Announce that torrent_name (added under category) completed."""

    async def announce_all_done(self) -> None:
        """Announce that this manager has no downloads of its own left."""


def format_eta(seconds) -> str:
    """Format an ETA in seconds as "Hh:Mm:Ss" — the single copy of this logic."""
    hours, remainder = divmod(max(int(seconds or 0), 0), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h:{minutes}m:{secs}s"


class DownloadManager:
    """Owns the torrent lifecycle for every download submitted through it."""

    def __init__(self, qbt_client, config, announcer, poll_interval: float = 15.0):
        self._qbt = qbt_client
        self._config = config
        self._announcer = announcer
        self._poll_interval = poll_interval
        self._submitted_hashes = set()  # discovered by a monitor, not yet completed
        self._pending = 0               # submitted, not yet visible in qBittorrent
        self._drained = True            # True: nothing of ours is active

    def submit(self, magnet_link: str, category: str) -> asyncio.Task:
        """Schedule the add + monitoring and return immediately."""
        # Mark the manager un-drained synchronously, before the task gets a
        # chance to run: an in-flight monitor must never see an empty manager
        # while a just-submitted torrent has not been added yet.
        self._drained = False
        task = asyncio.create_task(self._add_and_monitor(magnet_link, category))
        task.add_done_callback(self._log_task_error)
        return task

    def progress(self) -> list:
        """Rows shaped exactly like the /progress API contract."""
        torrents = self._qbt.torrents_info(status_filter='downloading')
        return [
            {
                "name": torrent.name,
                "progress": f"{torrent.progress * 100:.2f}%",
                "eta": format_eta(torrent.eta),
            }
            for torrent in torrents
        ]

    def cancel_all(self) -> list:
        """Delete every torrent qBittorrent knows about; return their names."""
        torrents = self._qbt.torrents_info()
        cancelled = []
        for torrent in torrents:
            self._qbt.torrents_delete(torrent_hashes=torrent.hash)
            cancelled.append(torrent.name)
        return cancelled

    async def _add_and_monitor(self, magnet_link: str, category: str) -> None:
        download_dir = os.path.join(self._config.base_download_dir, category)
        # Snapshot the hashes that exist before we add, so the completion
        # sweep can tell our torrents apart from everything else.
        hashes_before_add = {t.hash for t in self._qbt.torrents_info()}
        self._pending += 1
        try:
            self._qbt.torrents_add(urls=magnet_link, save_path=download_dir)
        except Exception as e:
            self._pending -= 1
            logging.error(f"Failed to add magnet link to qBittorrent: {e}")
            return
        logging.info(f"Magnet link added to qBittorrent under {category}.")
        await self._monitor(category, hashes_before_add)

    async def _monitor(self, category: str, hashes_before_add: set) -> None:
        polls = 0
        discovered = False
        while True:
            polls += 1
            downloading = self._qbt.torrents_info(status_filter='downloading')
            completed = self._qbt.torrents_info(status_filter='completed')

            new_hashes = {
                t.hash for t in list(downloading) + list(completed)
            } - hashes_before_add
            if new_hashes and not discovered:
                discovered = True
                self._pending = max(0, self._pending - 1)
            elif not discovered and polls > 1 and self._pending:
                # The torrent we added never showed up in qBittorrent (e.g. it
                # vanished right away); stop waiting for it so the manager
                # can drain.
                self._pending = max(0, self._pending - 1)
            if new_hashes:
                self._submitted_hashes |= new_hashes

            # Sweep only torrents THIS manager submitted.
            for torrent in completed:
                if torrent.hash in self._submitted_hashes:
                    self._qbt.torrents_delete(torrent_hashes=torrent.hash)
                    self._submitted_hashes.discard(torrent.hash)
                    logging.info(
                        f"{torrent.name} has been added to Bitbeard under {category}."
                    )
                    await self._announcer.announce_completed(
                        torrent.name, category
                    )

            # All-done is a manager-level event: announce it once per drain,
            # only when NOTHING submitted through this manager is still
            # active — not when one monitor task's own torrent is finished.
            if not self._pending and not self._submitted_hashes and not self._drained:
                self._drained = True
                logging.info("All downloads completed.")
                await self._announcer.announce_all_done()

            active_of_ours = [
                t for t in downloading if t.hash in self._submitted_hashes
            ]
            if not active_of_ours and (discovered or not self._pending):
                return

            await asyncio.sleep(self._poll_interval)

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logging.error(f"Download task failed: {exc}")
