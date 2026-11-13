"""Deep Download module: the torrent lifecycle (add -> poll -> announce -> delete).

Callers learn a small interface — ``submit``, ``progress``, ``cancel_all``,
``cancel_for`` and ``format_eta`` — and get the whole polling loop, hash-scoped
completion sweep, and announcement wiring behind it. The client, config,
announcer and poll interval are accepted as dependencies, never created here.

``submit`` takes the acting Account's name so Lifecycle Events stay
attributable, and an optional ``record`` — a ``DownloadRecord``-shaped
object the caller owns (``manager.quota`` builds real ones on the shared
store). The lifecycle drives the record through its states — discovered,
completed, cancelled, released — and never touches the store itself, so
tests can inject fakes. Without a record the lifecycle behaves exactly as
before (the torrent's files land straight in the Category directory).
"""

import asyncio
import logging
import os
from typing import Protocol


class Announcer(Protocol):
    """Seam for telling the outside world about lifecycle events."""

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
        self._actors: dict[str, str | None] = {}  # hash -> submitting Actor
        self._records: dict = {}        # hash -> the torrent's DownloadRecord

    def submit(
        self,
        magnet_link: str,
        category: str,
        actor: str | None = None,
        record=None,
    ) -> asyncio.Task:
        """Schedule the add + monitoring and return immediately.

        ``actor`` is the name of the Account that submitted the Download;
        it rides along on this torrent's Lifecycle Events. ``record`` is
        the Download's ownership record; when given, the torrent is added
        into the record's own save path and the record is driven through
        its lifecycle states as the torrent moves.
        """
        # Mark the manager un-drained synchronously, before the task gets a
        # chance to run: an in-flight monitor must never see an empty manager
        # while a just-submitted torrent has not been added yet.
        self._drained = False
        task = asyncio.create_task(
            self._add_and_monitor(magnet_link, category, actor, record)
        )
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
            record = self._records.pop(torrent.hash, None)
            if record is not None:
                record.cancelled()
            cancelled.append(torrent.name)
        self._actors.clear()
        return cancelled

    def cancel_for(self, save_path: str) -> list:
        """Delete only the torrent(s) parked at ``save_path``.

        This is the Download-level cancel the My Media page calls: the
        torrent's directory is matched against the Download record's save
        path, so one user's cancel never touches another's torrent. The
        caller settles the Download's record (``QuotaLedger.cancel``);
        the manager only sweeps qBittorrent and forgets its own mapping.
        """
        target = os.path.normpath(save_path)
        cancelled = []
        for torrent in self._qbt.torrents_info():
            torrent_path = getattr(torrent, "save_path", None)
            if torrent_path and os.path.normpath(torrent_path) == target:
                self._qbt.torrents_delete(torrent_hashes=torrent.hash)
                self._records.pop(torrent.hash, None)
                self._submitted_hashes.discard(torrent.hash)
                cancelled.append(torrent.name)
        return cancelled

    async def _add_and_monitor(
        self, magnet_link: str, category: str, actor: str | None, record
    ) -> None:
        download_dir = (
            record.save_path if record is not None
            else os.path.join(self._config.base_download_dir, category)
        )
        # The per-Download directory must exist before qBittorrent writes
        # into it (and so reconciliation can measure it even when empty).
        os.makedirs(download_dir, exist_ok=True)
        # Snapshot the hashes that exist before we add, so the completion
        # sweep can tell our torrents apart from everything else.
        hashes_before_add = {t.hash for t in self._qbt.torrents_info()}
        self._pending += 1
        try:
            self._qbt.torrents_add(urls=magnet_link, save_path=download_dir)
        except Exception as e:
            self._pending -= 1
            if record is not None:
                record.release()  # the submission never reached qBittorrent
            logging.error(f"Failed to add magnet link to qBittorrent: {e}")
            return
        logging.info(f"Magnet link added to qBittorrent under {category}.")
        await self._monitor(category, hashes_before_add, actor, record)

    async def _monitor(
        self, category: str, hashes_before_add: set, actor: str | None, record
    ) -> None:
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
                # The torrent showed up: this is the manager's "submitted"
                # moment, with the real name qBittorrent assigned.
                for torrent in list(downloading) + list(completed):
                    if torrent.hash in new_hashes:
                        self._actors[torrent.hash] = actor
                        self._records[torrent.hash] = record
                        if record is not None:
                            record.discovered(torrent.name)
                        await self._announcer.announce_submitted(
                            torrent.name,
                            category,
                            actor,
                            download_id=record.id if record is not None else None,
                        )
            elif not discovered and polls > 1 and self._pending:
                # The torrent we added never showed up in qBittorrent (e.g. it
                # vanished right away); stop waiting for it so the manager
                # can drain. The record stays until reconciliation resolves
                # it, in case the torrent still turns up later.
                self._pending = max(0, self._pending - 1)
            if new_hashes:
                self._submitted_hashes |= new_hashes

            # Sweep only torrents THIS manager submitted.
            for torrent in completed:
                if torrent.hash in self._submitted_hashes:
                    swept_record = self._records.pop(torrent.hash, None)
                    self._qbt.torrents_delete(torrent_hashes=torrent.hash)
                    self._submitted_hashes.discard(torrent.hash)
                    logging.info(
                        f"{torrent.name} has been added to Bitbeard under {category}."
                    )
                    if swept_record is not None:
                        swept_record.completed(
                            torrent.name, int(getattr(torrent, "size", 0) or 0)
                        )
                    await self._announcer.announce_completed(
                        torrent.name,
                        category,
                        self._actors.pop(torrent.hash, None),
                        download_id=swept_record.id if swept_record else None,
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
