"""Storage Quota accounting for the Download Manager.

This module owns the persistent side of a Download: the ``downloads``
ownership record and the owning Account's usage counter. The torrent
lifecycle (``manager.downloads``) drives a ``DownloadRecord`` through its
states and never touches SQL itself; everything from "insert the row" to
"recompute every counter from disk" lives here.

Why a record plus a counter: the record is the ownership decision of
record (qBittorrent state is never consulted for ownership — the manager
deletes completed torrents and the files stay owned), and the counter is
what the API compares against the Account's Storage Quota cap at the next
submission. The counter is adjusted at every lifecycle transition and kept
honest by ``reconcile``, which recomputes it from the records vs what is
actually on the Media Root and corrects drift from out-of-band changes
(manual deletions, qBittorrent/Plex side effects).

The store path, the Media Root and the qBittorrent client are accepted as
dependencies; tests point them at a tmp directory and a fake client.
"""

import logging
import os
import shutil
from contextlib import closing
from typing import Any

from shared import db

from .downloads import format_eta

logger = logging.getLogger(__name__)

#: Recorded size corrections (or counter corrections) beyond this many
#: bytes are logged as drift; smaller ones are corrected silently.
DRIFT_WARN_BYTES = 100 * 1024 ** 2


class ActiveTorrentError(Exception):
    """The Download's torrent is still Active — its paths are Protected.

    Users cancel an In-flight Download instead of deleting it; deleting a
    completed Download is safe because the completion sweep already removed
    the torrent from qBittorrent.
    """


class OutsideMediaRootError(Exception):
    """A Download's save path escapes the Media Root (hard sandbox)."""


class NotCompletedError(Exception):
    """Only completed Downloads can be renamed or moved.

    An In-flight Download's torrent still owns its directory in
    qBittorrent; the user cancels it instead.
    """


class PathTakenError(Exception):
    """The rename/move target already exists on disk or in a record."""


class DownloadRecord:
    """The manager's handle on one Download's ownership record.

    The torrent lifecycle calls these as the torrent moves: ``discovered``
    when qBittorrent knows the real name, ``completed`` when the files are
    on disk, ``cancelled`` when the torrent is swept by a cancel, and
    ``release`` when it never reached qBittorrent at all. Keeping the calls
    here — instead of SQL in the lifecycle — keeps the lifecycle ignorant
    of the store and lets tests inject fakes.
    """

    def __init__(self, ledger: "QuotaLedger", download_id: int, save_path: str):
        self.id = download_id
        self.save_path = save_path
        self._ledger = ledger

    def discovered(self, name: str) -> None:
        """The torrent appeared in qBittorrent with its real name."""
        self._ledger.discovered(self.id, name)

    def completed(self, name: str, size_bytes: int) -> None:
        """The torrent finished: record the actual size and settle quota."""
        self._ledger.complete(self.id, name, size_bytes)

    def cancelled(self) -> None:
        """The torrent was cancelled; it stops counting against the owner."""
        self._ledger.cancel(self.id)

    def release(self) -> None:
        """The torrent never made it into qBittorrent; undo the submission."""
        self._ledger.release(self.id)


class QuotaLedger:
    """The downloads records plus the per-Account usage counters.

    Every mutation keeps ``users.quota_used_bytes`` equal to the sum of the
    Account's record sizes (completed = on-disk actual, In-flight =
    projected, cancelled = 0); ``reconcile`` recomputes that sum from the
    records vs the Media Root and fixes whatever drifted.
    """

    def __init__(self, db_path: str, base_download_dir: str, qbt_client=None):
        self._db_path = db_path
        self._base_download_dir = base_download_dir
        self._qbt = qbt_client

    # --- Lifecycle transitions -------------------------------------------

    def open_download(
        self,
        *,
        account_id: int | None,
        discord_name: str | None,
        title: str | None,
        category: str,
        size_bytes: int,
    ) -> DownloadRecord:
        """Record a submission: the row, its per-Download path, the charge.

        The counter is charged immediately with the projected size so an
        In-flight Download counts against its owner from the moment it is
        submitted (the spec: no over-commit states are created). Unlinked
        Discord submitters have no Account to charge; their row carries the
        ``discord:<name>`` Actor the API attributed.
        """
        download_id = db.insert_download(
            self._db_path,
            account=account_id,
            discord_name=discord_name if account_id is None else None,
            name=title or "",
            category=category,
            save_path="",
            size_bytes=size_bytes,
        )
        save_path = os.path.join(
            self._base_download_dir, category, str(download_id)
        )
        db.set_download_save_path(self._db_path, download_id, save_path)
        if account_id is not None and size_bytes:
            db.adjust_quota_used(self._db_path, account_id, size_bytes)
        logger.info(
            "Download %s submitted under %s (%s bytes) for account=%s.",
            download_id, category, size_bytes, account_id,
        )
        return DownloadRecord(self, download_id, save_path)

    def discovered(self, download_id: int, name: str) -> None:
        """Record the torrent's real name once qBittorrent knows it."""
        db.update_download_name(self._db_path, download_id, name)

    def complete(self, download_id: int, name: str, size_bytes: int) -> None:
        """Mark completed and adjust the counter to the actual size.

        The row stays after the torrent is deleted from qBittorrent: the
        files remain on disk and stay owned — they are the owner's quota.
        """
        row = self._row(download_id)
        if row is None or row["status"] != "in_flight":
            return  # already settled (e.g. reconciled or cancelled)
        db.complete_download(
            self._db_path, download_id, name=name, size_bytes=size_bytes
        )
        if row["account"] is not None and size_bytes != row["size_bytes"]:
            db.adjust_quota_used(
                self._db_path, row["account"], size_bytes - row["size_bytes"]
            )
        logger.info("Download %s completed with %s bytes.", download_id, size_bytes)

    def cancel(self, download_id: int) -> None:
        """A cancelled torrent stops counting: zero its recorded size."""
        row = self._row(download_id)
        if row is None or row["status"] != "in_flight":
            return
        db.cancel_download(self._db_path, download_id)
        if row["account"] is not None and row["size_bytes"]:
            db.adjust_quota_used(
                self._db_path, row["account"], -row["size_bytes"]
            )

    def release(self, download_id: int) -> None:
        """Undo a submission whose torrent never reached qBittorrent."""
        row = self._row(download_id)
        if row is None or row["status"] != "in_flight":
            return
        db.delete_download_row(self._db_path, download_id)
        if row["account"] is not None and row["size_bytes"]:
            db.adjust_quota_used(
                self._db_path, row["account"], -row["size_bytes"]
            )
        logger.info("Download %s released (never reached qBittorrent).", download_id)

    # --- User-facing delete (the API surface arrives with ticket #6) ------

    def delete_download(self, download_id: int) -> dict | None:
        """Hard-delete a finished Download: files, record, quota.

        Returns the deleted record's fields, or ``None`` when no such
        Download exists. Refused while the torrent is still an Active
        Torrent (Protected Path) and for paths that escape the Media Root.
        """
        row = self._row(download_id)
        if row is None or not row["save_path"]:
            return None
        self._refuse_active_torrent(row["save_path"])
        self._remove_files(row["save_path"])
        db.delete_download_row(self._db_path, download_id)
        if row["account"] is not None and row["size_bytes"]:
            db.adjust_quota_used(
                self._db_path, row["account"], -row["size_bytes"]
            )
        logger.info("Download %s deleted; quota freed.", download_id)
        return dict(row)

    # --- User-facing media operations (ticket #6's My Media page) ----------

    def list_downloads(
        self,
        *,
        account_id: int | None = None,
        discord_name: str | None = None,
    ) -> list[dict]:
        """One Actor's Downloads for the My Media page, newest first.

        In-flight rows carry the torrent's live progress, matched by save
        path — the one thing qBittorrent and the record share (``None``
        while the torrent has not surfaced yet). Cancelled rows are not
        media — their partial files carry no quota — so they are not
        listed. Without an Actor to scope to, nothing is listed.
        """
        rows = db.downloads_for_owner(
            self._db_path, account_id=account_id, discord_name=discord_name
        )
        active = self._active_torrents_by_path()
        entries = []
        for row in rows:
            if row["status"] not in ("in_flight", "completed"):
                continue
            entry = {
                "id": row["id"],
                "name": row["name"],
                "category": row["category"],
                "size_bytes": row["size_bytes"],
                "status": row["status"],
                "submitted_at": row["submitted_at"],
                "completed_at": row["completed_at"],
            }
            if row["status"] == "in_flight":
                torrent = active.get(os.path.normpath(row["save_path"]))
                entry["progress"] = (
                    f"{torrent.progress * 100:.2f}%" if torrent else None
                )
                entry["eta"] = format_eta(torrent.eta) if torrent else None
            entries.append(entry)
        return entries

    def rename_download(self, download_id: int, new_name: str) -> dict | None:
        """Rename a completed Download: the record's name and its directory.

        The directory keeps its place under the Category; its leaf becomes
        the new name. Returns the updated record, or ``None`` when no such
        Download exists. Refused while the torrent is still an Active
        Torrent (Protected Path) and for names or paths that escape the
        Media Root.
        """
        row = self._row(download_id)
        if row is None or not row["save_path"]:
            return None
        self._refuse_active_torrent(row["save_path"])
        if row["status"] != "completed":
            raise NotCompletedError(
                # USER-FACING
                f"Download {download_id} is not completed yet; "
                "cancel it instead of renaming it."
            )
        target = os.path.join(
            os.path.dirname(os.path.normpath(row["save_path"])), new_name
        )
        self._relocate(row, target)
        db.update_download_name(self._db_path, download_id, new_name)
        logger.info("Download %s renamed to %s.", download_id, new_name)
        return dict(self._row(download_id))

    def move_download(self, download_id: int, category: str) -> dict | None:
        """Move a completed Download to another Category: record + disk.

        The directory keeps its leaf name; the files move under the new
        Category's directory and the record follows. Quota is unchanged —
        the same bytes, just shelved elsewhere. Guards as for rename.
        """
        row = self._row(download_id)
        if row is None or not row["save_path"]:
            return None
        self._refuse_active_torrent(row["save_path"])
        if row["status"] != "completed":
            raise NotCompletedError(
                # USER-FACING
                f"Download {download_id} is not completed yet; "
                "cancel it instead of moving it."
            )
        leaf = os.path.basename(os.path.normpath(row["save_path"]))
        target = os.path.join(self._base_download_dir, category, leaf)
        self._relocate(row, target)
        db.update_download_category(self._db_path, download_id, category)
        logger.info(
            "Download %s moved to the %s Category.", download_id, category
        )
        return dict(self._row(download_id))

    # --- Reconciliation ----------------------------------------------------

    def reconcile(self) -> dict:
        """Recompute usage from the records vs on-disk state; correct drift.

        Per record: a completed Download's on-disk total replaces its
        recorded size (files edited or removed out-of-band); an In-flight
        Download whose torrent has left qBittorrent is resolved — completed
        from what exists on disk, or released when nothing materialized,
        which is how a manager restart stops a stuck row from counting
        forever. Every Account's counter is then recomputed from its
        records. Corrections beyond ``DRIFT_WARN_BYTES`` are logged.
        """
        summary: dict = {
            "size_corrections": [],
            "resolved_completed": [],
            "released": [],
            "counter_corrections": {},
        }
        active_paths = self._active_save_paths()

        for row in db.all_downloads(self._db_path):
            if row["status"] == "completed" and row["save_path"]:
                self._correct_completed_size(row, summary)
            elif row["status"] == "in_flight" and row["save_path"]:
                if os.path.normpath(row["save_path"]) in active_paths:
                    continue  # still an Active Torrent; its monitor owns it
                disk = self._disk_usage(row["save_path"])
                if disk > 0:
                    db.complete_download(
                        self._db_path,
                        row["id"],
                        name=row["name"],
                        size_bytes=disk,
                    )
                    summary["resolved_completed"].append(row["id"])
                else:
                    db.delete_download_row(self._db_path, row["id"])
                    summary["released"].append(row["id"])

        self._recompute_counters(summary)
        return summary

    def _correct_completed_size(self, row, summary: dict) -> None:
        """Replace a completed Download's recorded size with the on-disk one."""
        disk = self._disk_usage(row["save_path"])
        if disk == row["size_bytes"]:
            return
        drift = disk - row["size_bytes"]
        if abs(drift) > DRIFT_WARN_BYTES:
            logger.warning(
                "Download %s (%s): recorded size drifts %s bytes from disk "
                "(%s recorded, %s on disk); correcting.",
                row["id"], row["name"], drift, row["size_bytes"], disk,
            )
        db.set_download_size(self._db_path, row["id"], disk)
        summary["size_corrections"].append({"id": row["id"], "drift": drift})

    def _recompute_counters(self, summary: dict) -> None:
        """Set every Account's counter to its records' sum."""
        usage = db.download_usage_by_account(self._db_path)
        for account_id in set(usage) | set(db.all_account_ids(self._db_path)):
            expected = usage.get(account_id, 0)
            with closing(db.connect(self._db_path)) as connection:
                current = connection.execute(
                    "SELECT quota_used_bytes FROM users WHERE id = ?",
                    (account_id,),
                ).fetchone()
            if current is None or current["quota_used_bytes"] == expected:
                continue
            drift = expected - current["quota_used_bytes"]
            if abs(drift) > DRIFT_WARN_BYTES:
                logger.warning(
                    "Account %s usage counter drifts %s bytes "
                    "(%s counted, %s on record); correcting.",
                    account_id, drift, current["quota_used_bytes"], expected,
                )
            db.set_quota_used(self._db_path, account_id, expected)
            summary["counter_corrections"][account_id] = drift

    # --- Helpers ------------------------------------------------------------

    def _row(self, download_id: int):
        return db.get_download(self._db_path, download_id)

    def _active_torrents_by_path(self) -> dict[str, Any]:
        """Active Torrents keyed by their normalized save path."""
        if self._qbt is None:
            return {}
        return {
            os.path.normpath(torrent.save_path): torrent
            for torrent in self._qbt.torrents_info()
            if getattr(torrent, "save_path", None)
        }

    def _active_save_paths(self) -> set[str]:
        """Save paths qBittorrent currently holds (Active Torrents)."""
        return set(self._active_torrents_by_path())

    def _refuse_active_torrent(self, save_path: str) -> None:
        """Protected Path guard: refuse while the torrent is still Active."""
        if self._qbt is None:
            return
        torrent = self._active_torrents_by_path().get(
            os.path.normpath(save_path)
        )
        if torrent is not None:
            raise ActiveTorrentError(
                # USER-FACING
                f"{save_path} belongs to an Active Torrent; its paths are "
                "Protected — cancel the torrent first."
            )

    def _relocate(self, row, target: str) -> None:
        """Move a Download's directory and repoint its record's save path.

        Both paths are sandbox-checked, the target must be free of any
        other Download's record and of anything on disk, and quota never
        changes — the same bytes, a different address.
        """
        source = row["save_path"]
        self._ensure_inside_media_root(source)
        self._ensure_inside_media_root(target)
        if os.path.normpath(source) == os.path.normpath(target):
            return  # the "rename" is the record's name only
        occupant = db.get_download_by_save_path(self._db_path, target)
        if occupant is not None and occupant["id"] != row["id"]:
            raise PathTakenError(
                # USER-FACING
                f"{target} is already the save path of another Download."
            )
        if os.path.exists(target):
            # USER-FACING
            raise PathTakenError(f"{target} already exists on the Media Root.")
        if not os.path.exists(source):
            # The files are already gone (out-of-band change); the record
            # simply repoints.
            db.set_download_save_path(self._db_path, row["id"], target)
            return
        shutil.move(source, target)
        db.set_download_save_path(self._db_path, row["id"], target)

    def _ensure_inside_media_root(self, save_path: str) -> None:
        """Hard sandbox: no file operation may escape the Media Root."""
        root = os.path.realpath(self._base_download_dir)
        target = os.path.realpath(save_path)
        if target != root and not target.startswith(root + os.sep):
            raise OutsideMediaRootError(
                # USER-FACING
                f"{save_path} escapes the Media Root."
            )

    def _remove_files(self, save_path: str) -> None:
        """Remove a Download's files, sandboxed to the Media Root."""
        self._ensure_inside_media_root(save_path)
        target = os.path.realpath(save_path)
        if os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.exists(target):
            os.remove(target)

    def _disk_usage(self, save_path: str) -> int:
        """Total bytes of a Download's files; 0 when nothing is there."""
        total = 0
        for directory, _, files in os.walk(save_path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(directory, name))
                except OSError:
                    continue
        return total
