"""Admin file management for the Media Root (ticket #10).

The manager owns the media volume (ADR-0001), so the Admin Panel's file
browser lives here: browse the tree under a path, hard-delete, rename and
move — every operation sandboxed to the Media Root and refused on
Protected Paths (any file or directory inside an Active Torrent's save
path, and any ancestor of one, since touching it would destroy the
torrent's files).

Paths cross the API as relative, "/"-separated paths under the Media Root
(the same shape the Admin Panel navigates with); this module is the only
place that joins them onto the Media Root, so traversal and escapes are
rejected by construction and re-checked against the real root afterwards.

The disk is an injected seam: the default ``RealDisk`` is a thin os/shutil
wrapper, tests hand in a ``FakeDisk`` and touch no real filesystem.
"""

import logging
import os
import shutil

from shared import db

from .quota import OutsideMediaRootError, PathTakenError

logger = logging.getLogger(__name__)


class ProtectedPathError(Exception):
    """The path overlaps an Active Torrent's save path — it is Protected."""


class InvalidPathError(Exception):
    """The path (or a rename's new name) cannot name anything on disk."""


def _validate_relative(relative: str) -> list[str]:
    """Split a relative Media Root path into safe components.

    Backslashes are treated as separators (a Windows habit must not smuggle
    a name across directories), empty and ``.`` components are dropped, and
    anything else that could escape — ``..``, null bytes, absolute paths —
    is refused outright.
    """
    if "\x00" in relative:
        raise OutsideMediaRootError(
            # USER-FACING
            f"{relative!r} escapes the Media Root."
        )
    parts = [
        component
        for component in relative.replace("\\", "/").split("/")
        if component and component != "."
    ]
    if relative.startswith(("/", "\\")):
        raise OutsideMediaRootError(
            f"{relative!r} escapes the Media Root."
        )
    if ".." in parts:
        raise OutsideMediaRootError(
            f"{relative!r} escapes the Media Root."
        )
    return parts


def validate_entry_name(name: str) -> str:
    """Validate a rename target as a single on-disk name."""
    cleaned = (name or "").strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
    ):
        raise InvalidPathError(
            # USER-FACING
            f"{name!r} cannot name a file or directory on the Media Root."
        )
    return cleaned


class Disk:
    """The file-system operations the file manager needs.

    The seam tests inject a fake into; the real implementation below is a
    thin os/shutil wrapper so the module's logic stays testable without a
    filesystem.
    """

    def is_dir(self, path: str) -> bool:
        raise NotImplementedError

    def is_file(self, path: str) -> bool:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def entries(self, path: str) -> list[str]:
        raise NotImplementedError

    def size(self, path: str) -> int:
        raise NotImplementedError

    def remove(self, path: str) -> None:
        raise NotImplementedError

    def rename(self, source: str, target: str) -> None:
        raise NotImplementedError


class RealDisk(Disk):
    """The real filesystem, sandboxed callers notwithstanding."""

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def entries(self, path: str) -> list[str]:
        return list(os.listdir(path))

    def size(self, path: str) -> int:
        return os.path.getsize(path)

    def remove(self, path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        else:
            # USER-FACING
            raise FileNotFoundError(f"No file or directory at {path}.")

    def rename(self, source: str, target: str) -> None:
        if not os.path.exists(source):
            raise FileNotFoundError(f"No file or directory at {source}.")
        shutil.move(source, target)


class FileManager:
    """Browse, delete, rename and move inside the Media Root.

    Deep module: callers hand in relative paths and get answers; the
    sandbox, the Protected Path guards and the Download-record bookkeeping
    (a whole Download's save path being removed drops its record and frees
    the owner's Storage Quota; a rename/move repoints the record so the
    periodic reconcile keeps accounting for its bytes) all stay inside.
    """

    def __init__(
        self,
        db_path: str,
        base_download_dir: str,
        qbt_client=None,
        disk: Disk | None = None,
    ):
        self._db_path = db_path
        self._base_download_dir = base_download_dir
        self._qbt = qbt_client
        self._disk = disk or RealDisk()

    # --- Reading -----------------------------------------------------------

    def browse(self, relative: str = "") -> dict:
        """One directory of the Media Root tree, entries sorted.

        Directories come first, then files, both by name; sizes are bytes
        (``0`` for directories — their trees are not walked for a listing).
        Unknown paths raise ``FileNotFoundError``, which the service maps
        to a 404.
        """
        _, absolute = self._resolve(relative)
        if not self._disk.is_dir(absolute):
            # USER-FACING
            raise FileNotFoundError(f"No file or directory at {relative!r}.")
        entries = []
        for name in self._disk.entries(absolute):
            entry_absolute = os.path.join(absolute, name)
            is_dir = self._disk.is_dir(entry_absolute)
            entries.append(
                {
                    "name": name,
                    "is_dir": is_dir,
                    "size_bytes": 0 if is_dir else self._disk.size(entry_absolute),
                }
            )
        entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
        return {"path": self._normalize(relative), "entries": entries}

    # --- Acting ------------------------------------------------------------

    def delete(self, relative: str) -> dict:
        """Hard-delete a file or directory tree, sandboxed and guarded.

        When the path is a whole Download's save path, the ownership record
        is dropped and the owner's Storage Quota is freed immediately;
        partial deletions inside a Download are left to the periodic
        reconciliation, which recomputes the recorded size from disk.
        """
        normalized, absolute = self._resolve(relative)
        if not self._disk.exists(absolute):
            raise FileNotFoundError(f"No file or directory at {relative!r}.")
        self._refuse_protected(normalized, absolute)
        self._disk.remove(absolute)
        self._drop_record_at(absolute)
        logger.info("File manager: deleted %s.", normalized)
        return {"deleted": normalized}

    def rename(self, relative: str, new_name: str) -> dict:
        """Rename a file or directory, keeping its place in the tree."""
        normalized, absolute = self._resolve(relative)
        name = validate_entry_name(new_name)
        parent = os.path.dirname(absolute)
        _, target = self._resolve_join(parent, name)
        if os.path.normpath(absolute) == os.path.normpath(target):
            return {"renamed": normalized, "to": normalized}
        self._refuse_protected(normalized, absolute)
        self._refuse_protected_taken(target)
        self._refuse_taken(target)
        self._disk.rename(absolute, target)
        self._repoint_record(absolute, target)
        to_relative = self._normalize(
            os.path.relpath(target, self._base_download_dir)
        )
        logger.info("File manager: renamed %s to %s.", normalized, to_relative)
        return {"renamed": normalized, "to": to_relative}

    def move(self, relative: str, target_dir: str) -> dict:
        """Move a file or directory under another directory of the tree."""
        normalized, absolute = self._resolve(relative)
        _, target_parent = self._resolve(target_dir)
        if not self._disk.is_dir(target_parent):
            # USER-FACING
            raise FileNotFoundError(f"No file or directory at {target_dir!r}.")
        target = os.path.join(target_parent, os.path.basename(absolute))
        if os.path.normpath(absolute) == os.path.normpath(target):
            return {"moved": normalized, "to": normalized}
        self._refuse_protected(normalized, absolute)
        self._refuse_protected_taken(target)
        self._refuse_taken(target)
        self._disk.rename(absolute, target)
        self._repoint_record(absolute, target)
        to_relative = self._normalize(
            os.path.relpath(target, self._base_download_dir)
        )
        logger.info("File manager: moved %s to %s.", normalized, to_relative)
        return {"moved": normalized, "to": to_relative}

    # --- Path resolution -----------------------------------------------------

    def _resolve(self, relative: str) -> tuple[str, str]:
        """Validate a relative path; return (normalized, absolute) forms."""
        parts = _validate_relative(relative or "")
        absolute = self._join(parts)
        root = os.path.realpath(self._base_download_dir)
        resolved = os.path.realpath(absolute)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise OutsideMediaRootError(
                f"{relative!r} escapes the Media Root."
            )
        return self._normalize(relative), absolute

    def _resolve_join(self, parent_absolute: str, name: str) -> tuple[str, str]:
        """Resolve ``name`` inside an already-resolved parent directory."""
        absolute = os.path.join(parent_absolute, name)
        root = os.path.realpath(self._base_download_dir)
        resolved = os.path.realpath(absolute)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise OutsideMediaRootError(
                # USER-FACING
                f"{name!r} escapes the Media Root."
            )
        try:
            normalized = os.path.relpath(absolute, self._base_download_dir)
        except ValueError:  # different drive (Windows)
            raise OutsideMediaRootError(
                f"{name!r} escapes the Media Root."
            ) from None
        return normalized.replace(os.sep, "/"), absolute

    def _join(self, parts: list[str]) -> str:
        return os.path.join(
            os.path.abspath(self._base_download_dir), *parts
        ) if parts else os.path.abspath(self._base_download_dir)

    def _normalize(self, relative: str) -> str:
        """The "/"-joined relative form the Frontends navigate with."""
        parts = _validate_relative(relative or "")
        return "/".join(parts)

    # --- Guards --------------------------------------------------------------

    def _active_save_paths(self) -> set[str]:
        """Save paths qBittorrent currently holds (Active Torrents)."""
        if self._qbt is None:
            return set()
        return {
            os.path.normpath(torrent.save_path)
            for torrent in self._qbt.torrents_info()
            if getattr(torrent, "save_path", None)
        }

    def _refuse_protected(self, normalized: str, absolute: str) -> None:
        """Refuse a path that overlaps an Active Torrent's save path.

        Both directions are Protected: inside a save path (the torrent's
        files) and above one (deleting the whole directory would destroy
        the torrent with it).
        """
        candidate = os.path.normpath(absolute)
        for save_path in self._active_save_paths():
            if (
                candidate == save_path
                or candidate.startswith(save_path + os.sep)
                or save_path.startswith(candidate + os.sep)
            ):
                raise ProtectedPathError(
                    # USER-FACING
                    f"{normalized!r} is inside an Active Torrent's save "
                    "path; its paths are Protected — cancel the torrent "
                    "first."
                )

    def _refuse_protected_taken(self, target_absolute: str) -> None:
        """Refuse a rename/move target that is itself inside a save path."""
        if self._qbt is None:
            return
        for save_path in self._active_save_paths():
            candidate = os.path.normpath(target_absolute)
            if candidate == save_path or candidate.startswith(
                save_path + os.sep
            ):
                raise ProtectedPathError(
                    # USER-FACING
                    "The target is inside an Active Torrent's save path; "
                    "its paths are Protected."
                )

    def _refuse_taken(self, target_absolute: str) -> None:
        """The target must be free on disk and in the records."""
        if self._disk.exists(target_absolute):
            raise PathTakenError(
                # USER-FACING
                f"{target_absolute} already exists on the Media Root."
            )
        record = db.get_download_by_save_path(
            self._db_path, target_absolute
        )
        if record is not None:
            raise PathTakenError(
                # USER-FACING
                f"{target_absolute} is already the save path of another "
                "Download."
            )

    # --- Download-record bookkeeping ------------------------------------------

    def _download_at(self, absolute: str) -> dict | None:
        """The Download record parked at a path, if any.

        Records store the save path the manager itself wrote, so an exact
        match usually lands; the normalized comparison covers records whose
        stored form drifted (a user rename wrote a different-but-equal
        spelling of the same directory).
        """
        target = os.path.normpath(absolute)
        row = db.get_download_by_save_path(self._db_path, absolute)
        if row is not None:
            return dict(row)
        for row in db.all_downloads(self._db_path):
            if row["save_path"] and os.path.normpath(row["save_path"]) == target:
                return dict(row)
        return None

    def _drop_record_at(self, absolute: str) -> None:
        """A whole Download's save path was removed: drop record + quota."""
        row = self._download_at(absolute)
        if row is None:
            return
        db.delete_download_row(self._db_path, row["id"])
        if row["account"] is not None and row["size_bytes"]:
            db.adjust_quota_used(
                self._db_path, row["account"], -row["size_bytes"]
            )
        logger.info(
            "Download %s's save path was removed by the file manager; "
            "record dropped and quota freed.",
            row["id"],
        )

    def _repoint_record(self, source_absolute: str, target_absolute: str) -> None:
        """A Download's directory was renamed/moved: the record follows.

        Quota is unchanged — the same bytes, a different address — and the
        record's name is left alone: the file manager renames paths, the
        Download's title is the owner's business (My Media renames it).
        """
        row = self._download_at(source_absolute)
        if row is None:
            return
        db.set_download_save_path(self._db_path, row["id"], target_absolute)
        logger.info(
            "Download %s's save path repointed to %s by the file manager.",
            row["id"], target_absolute,
        )
