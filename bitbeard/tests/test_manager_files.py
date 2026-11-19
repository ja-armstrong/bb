"""Tests for the Admin file manager on the Media Root: ticket #10.

The manager owns the media volume, so the sandbox, the Protected Path
guards (Active Torrents) and the Download-record bookkeeping live in
``manager.files``. The disk is the injected seam: the FakeDisk unit tests
touch no filesystem, the endpoint tests use a tmp directory as the Media
Root with the real disk.
"""

import os
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from manager.config import Config
from manager.files import FileManager, ProtectedPathError
from manager.main import create_app
from manager.quota import (
    OutsideMediaRootError,
    PathTakenError,
    QuotaLedger,
)
from shared import db


def make_config(tmp_path):
    return Config(
        qbit_username="admin",
        qbit_password="hunter2",
        base_download_dir=str(tmp_path / "media"),
        jackett_api_key="jackett-key",
        service_token="service-token",
        db_path=str(tmp_path / "bitbeard.db"),
        reconcile_interval=0.0,
    )


# --- The fake disk ------------------------------------------------------------


class FakeDisk:
    """In-memory tree: a set of directories and a name->size file map."""

    def __init__(self):
        self._dirs = set()
        self._files = {}
        self.calls = []

    # Test helpers -----------------------------------------------------

    def add_dir(self, path):
        path = os.path.normpath(path)
        self._dirs.add(path)
        parent = os.path.dirname(path)
        if parent and parent not in self._dirs:
            self.add_dir(parent)

    def add_file(self, path, size=1024):
        path = os.path.normpath(path)
        self.add_dir(os.path.dirname(path))
        self._files[path] = size

    # The Disk seam ----------------------------------------------------

    def is_dir(self, path):
        return os.path.normpath(path) in self._dirs

    def is_file(self, path):
        return os.path.normpath(path) in self._files

    def exists(self, path):
        p = os.path.normpath(path)
        return p in self._dirs or p in self._files

    def entries(self, path):
        p = os.path.normpath(path)
        names = set()
        for directory in self._dirs:
            if os.path.dirname(directory) == p:
                names.add(os.path.basename(directory))
        for file_path in self._files:
            if os.path.dirname(file_path) == p:
                names.add(os.path.basename(file_path))
        return sorted(names)

    def size(self, path):
        return self._files[os.path.normpath(path)]

    def remove(self, path):
        p = os.path.normpath(path)
        self.calls.append(("remove", p))
        if p in self._files:
            del self._files[p]
        elif p in self._dirs:
            prefix = p + os.sep
            self._dirs = {
                d for d in self._dirs if d != p and not d.startswith(prefix)
            }
            self._files = {
                f: s for f, s in self._files.items() if not f.startswith(prefix)
            }
        else:
            raise FileNotFoundError(f"No file or directory at {path}.")

    def rename(self, source, target):
        s, t = os.path.normpath(source), os.path.normpath(target)
        self.calls.append(("rename", s, t))
        if not self.exists(s):
            raise FileNotFoundError(f"No file or directory at {source}.")
        if self.exists(t):
            raise FileExistsError(f"{target} already exists.")
        if s in self._dirs:
            prefix = s + os.sep
            self._dirs.discard(s)
            self._dirs.add(t)
            for directory in list(self._dirs):
                if directory.startswith(prefix):
                    self._dirs.discard(directory)
                    self._dirs.add(os.path.join(t, directory[len(prefix):]))
            for file_path in list(self._files):
                if file_path.startswith(prefix):
                    new_path = os.path.join(t, file_path[len(prefix):])
                    self._files[new_path] = self._files.pop(file_path)
        else:
            self._files[t] = self._files.pop(s)


class FakeTorrent:
    def __init__(self, hash, save_path):
        self.hash = hash
        self.name = "Test Torrent"
        self.save_path = save_path


class ActiveQbt:
    """One Active Torrent parked at a save path."""

    def __init__(self, save_path):
        self._torrent = FakeTorrent("hash1", os.path.normpath(save_path))

    def torrents_info(self, status_filter=None):
        return [self._torrent]


def make_files(tmp_path, qbt=None, disk=None):
    """A FileManager over a fake disk; returns (files, disk)."""
    db_path = str(tmp_path / "bitbeard.db")
    db.initialize(db_path)
    base = str(tmp_path / "media")
    disk = disk or FakeDisk()
    return FileManager(db_path, base, qbt_client=qbt, disk=disk), disk


def disk_path(tmp_path, *relative_parts):
    return os.path.join(str(tmp_path / "media"), *relative_parts)


def quota_used(db_path, account_id):
    with closing(db.connect(db_path)) as connection:
        return connection.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (account_id,)
        ).fetchone()[0]


def make_account(db_path, size_bytes=0):
    with closing(db.connect(db_path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES ('deckhand', 'not-a-real-hash', 'user', ?)
            """,
            (db.utc_now(),),
        )
        connection.commit()
    return cursor.lastrowid


# --- Browse -----------------------------------------------------------------


def test_browse_lists_directories_first_then_files(tmp_path):
    files, disk = make_files(tmp_path)
    base = disk_path(tmp_path)
    disk.add_dir(base)
    disk.add_dir(os.path.join(base, "Shows"))
    disk.add_file(os.path.join(base, "zebra.bin"), size=2048)
    disk.add_dir(os.path.join(base, "Movies"))
    disk.add_file(os.path.join(base, "alpha.txt"), size=10)

    listing = files.browse("")

    assert listing["path"] == ""
    assert [(entry["name"], entry["is_dir"]) for entry in listing["entries"]] == [
        ("Movies", True),
        ("Shows", True),
        ("alpha.txt", False),
        ("zebra.bin", False),
    ]
    assert listing["entries"][2]["size_bytes"] == 10


def test_browse_of_a_subdirectory_returns_its_relative_path(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_dir(disk_path(tmp_path, "Movies", "Scene"))

    listing = files.browse("Movies/Scene")

    assert listing["path"] == "Movies/Scene"
    assert listing["entries"] == []


def test_browse_of_an_unknown_path_is_refused(tmp_path):
    files, _ = make_files(tmp_path)

    with pytest.raises(FileNotFoundError):
        files.browse("Movies/Missing")


def test_browse_of_a_file_is_refused(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "file.bin"))

    with pytest.raises((FileNotFoundError, NotADirectoryError)):
        files.browse("Movies/file.bin")


# --- The sandbox -------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "../outside",
        "../../etc/passwd",
        "Movies/../../outside",
        "/etc/passwd",
        "Movies/file\x00.bin",
    ],
)
def test_operations_refuse_paths_that_escape_the_media_root(tmp_path, relative):
    files, disk = make_files(tmp_path)

    with pytest.raises(OutsideMediaRootError):
        files.browse(relative)
    with pytest.raises(OutsideMediaRootError):
        files.delete(relative)
    with pytest.raises(OutsideMediaRootError):
        files.rename(relative, "x")
    with pytest.raises(OutsideMediaRootError):
        files.move(relative, "Movies")

    assert disk.calls == []


def test_operations_tolerate_dot_and_slash_noise(tmp_path):
    files, _ = make_files(tmp_path)
    disk = files._disk
    disk.add_dir(disk_path(tmp_path, "Movies"))

    listing = files.browse("./Movies/")

    assert listing["path"] == "Movies"


# --- Delete -------------------------------------------------------------------


def test_delete_removes_a_file(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "file.bin"))

    result = files.delete("Movies/file.bin")

    assert result == {"deleted": "Movies/file.bin"}
    assert not disk.exists(disk_path(tmp_path, "Movies", "file.bin"))


def test_delete_removes_a_directory_tree(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "inner", "file.bin"))

    files.delete("Movies")

    assert not disk.exists(disk_path(tmp_path, "Movies"))


def test_delete_of_an_unknown_path_is_refused(tmp_path):
    files, _ = make_files(tmp_path)

    with pytest.raises(FileNotFoundError):
        files.delete("Movies/Missing")


# --- Rename -------------------------------------------------------------------


def test_rename_moves_a_directory_within_its_parent(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "Old Name", "file.bin"))

    result = files.rename("Movies/Old Name", "New Name")

    assert result == {"renamed": "Movies/Old Name", "to": "Movies/New Name"}
    assert disk.is_file(disk_path(tmp_path, "Movies", "New Name", "file.bin"))
    assert not disk.exists(disk_path(tmp_path, "Movies", "Old Name"))


def test_rename_of_a_file_keeps_the_directory(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "old.txt"))

    files.rename("Movies/old.txt", "new.txt")

    assert disk.is_file(disk_path(tmp_path, "Movies", "new.txt"))
    assert disk.is_dir(disk_path(tmp_path, "Movies"))


def test_rename_refuses_a_taken_target(tmp_path):
    files, _ = make_files(tmp_path)
    disk = files._disk
    disk.add_dir(disk_path(tmp_path, "Movies"))
    disk.add_dir(disk_path(tmp_path, "Movies", "A"))
    disk.add_file(disk_path(tmp_path, "Movies", "B", "keep.bin"))

    with pytest.raises(PathTakenError):
        files.rename("Movies/A", "B")


def test_rename_refuses_names_that_could_escape(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_dir(disk_path(tmp_path, "Movies"))

    for bad_name in ("../escape", "sub/inner", "back\\slash", "", "..", "."):
        with pytest.raises(Exception) as excinfo:
            files.rename("Movies", bad_name)
        assert not isinstance(excinfo.value, PathTakenError)

    assert disk.calls == []


def test_rename_to_the_same_name_is_a_no_op(tmp_path):
    files, _ = make_files(tmp_path)
    disk = files._disk
    disk.add_dir(disk_path(tmp_path, "Movies"))

    result = files.rename("Movies", "Movies")

    assert result == {"renamed": "Movies", "to": "Movies"}
    assert disk.calls == []


# --- Move ---------------------------------------------------------------------


def test_move_relocates_into_another_directory(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "film.bin"))
    disk.add_dir(disk_path(tmp_path, "Shows"))

    result = files.move("Movies/film.bin", "Shows")

    assert result == {"moved": "Movies/film.bin", "to": "Shows/film.bin"}
    assert disk.is_file(disk_path(tmp_path, "Shows", "film.bin"))
    assert not disk.exists(disk_path(tmp_path, "Movies", "film.bin"))


def test_move_into_a_missing_directory_is_refused(tmp_path):
    files, _ = make_files(tmp_path)
    disk = files._disk
    disk.add_file(disk_path(tmp_path, "Movies", "film.bin"))

    with pytest.raises(FileNotFoundError):
        files.move("Movies/film.bin", "Shows")


def test_move_onto_an_occupied_target_is_refused(tmp_path):
    files, _ = make_files(tmp_path)
    disk = files._disk
    disk.add_file(disk_path(tmp_path, "Movies", "film.bin"))
    disk.add_file(disk_path(tmp_path, "Shows", "film.bin"))

    with pytest.raises(PathTakenError):
        files.move("Movies/film.bin", "Shows")


def test_move_into_the_root(tmp_path):
    files, disk = make_files(tmp_path)
    disk.add_file(disk_path(tmp_path, "Movies", "film.bin"))

    result = files.move("Movies/film.bin", "")

    assert result == {"moved": "Movies/film.bin", "to": "film.bin"}
    assert disk.is_file(disk_path(tmp_path, "film.bin"))


# --- Protected Paths ------------------------------------------------------------


def test_delete_of_a_file_inside_an_active_torrents_save_path_is_refused(
    tmp_path,
):
    save_path = disk_path(tmp_path, "Movies", "5")
    files, _ = make_files(tmp_path, qbt=ActiveQbt(save_path))
    files._disk.add_file(os.path.join(save_path, "file.bin"))

    with pytest.raises(ProtectedPathError):
        files.delete("Movies/5/file.bin")

    assert files._disk.exists(os.path.join(save_path, "file.bin"))


def test_delete_of_the_active_torrents_save_path_itself_is_refused(tmp_path):
    save_path = disk_path(tmp_path, "Movies", "5")
    files, _ = make_files(tmp_path, qbt=ActiveQbt(save_path))
    files._disk.add_dir(save_path)

    with pytest.raises(ProtectedPathError):
        files.delete("Movies/5")


def test_delete_of_an_ancestor_of_a_save_path_is_refused(tmp_path):
    """Deleting the whole Category would destroy the torrent with it."""
    save_path = disk_path(tmp_path, "Movies", "5")
    files, _ = make_files(tmp_path, qbt=ActiveQbt(save_path))
    files._disk.add_dir(save_path)

    with pytest.raises(ProtectedPathError):
        files.delete("Movies")


def test_sibling_paths_are_not_protected(tmp_path):
    save_path = disk_path(tmp_path, "Movies", "5")
    files, _ = make_files(tmp_path, qbt=ActiveQbt(save_path))
    files._disk.add_dir(disk_path(tmp_path, "Movies", "6"))

    files.delete("Movies/6")  # no refusal


def test_rename_and_move_refuse_protected_targets(tmp_path):
    save_path = disk_path(tmp_path, "Movies", "5")
    files, _ = make_files(tmp_path, qbt=ActiveQbt(save_path))
    disk = files._disk
    disk.add_dir(disk_path(tmp_path, "Movies", "A"))
    disk.add_file(os.path.join(save_path, "file.bin"))
    disk.add_file(disk_path(tmp_path, "Shows", "film.bin"))

    with pytest.raises(ProtectedPathError):
        files.rename("Movies/A", "5")
    with pytest.raises(ProtectedPathError):
        files.move("Shows/film.bin", "Movies/5")


def test_rename_of_a_source_inside_a_save_path_is_refused(tmp_path):
    save_path = disk_path(tmp_path, "Movies", "5")
    files, _ = make_files(tmp_path, qbt=ActiveQbt(save_path))
    files._disk.add_file(os.path.join(save_path, "file.bin"))

    with pytest.raises(ProtectedPathError):
        files.rename("Movies/5/file.bin", "other.bin")


# --- Download-record bookkeeping ----------------------------------------------


def make_record(tmp_path, *, size_bytes=1024):
    """A completed Download record with real files on the real disk."""
    config = Config(
        qbit_username="a",
        qbit_password="b",
        base_download_dir=str(tmp_path / "media"),
        jackett_api_key="k",
        service_token="t",
        db_path=str(tmp_path / "bitbeard.db"),
        reconcile_interval=0.0,
    )
    db.initialize(config.db_path)
    ledger = QuotaLedger(config.db_path, config.base_download_dir)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title="Some Movies",
        category="Movies", size_bytes=size_bytes,
    )
    record.completed("Some Movies", size_bytes)
    save_path = record.save_path
    os.makedirs(save_path, exist_ok=True)
    with open(os.path.join(save_path, "file.bin"), "wb") as handle:
        handle.write(b"x" * size_bytes)
    return record, account_id, config.db_path


def test_whole_download_delete_drops_the_record_and_frees_quota(tmp_path):
    record, account_id, db_path = make_record(tmp_path)

    # The FileManager shares the store and the Media Root with the ledger.
    manager = FileManager(
        str(tmp_path / "bitbeard.db"),
        str(tmp_path / "media"),
    )
    relative = os.path.relpath(record.save_path, str(tmp_path / "media"))
    manager.delete(relative.replace(os.sep, "/"))

    assert not os.path.exists(record.save_path)
    with closing(db.connect(db_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM downloads WHERE id = ?", (record.id,)
        ).fetchone()[0] == 0
    assert quota_used(db_path, account_id) == 0


def test_partial_delete_inside_a_download_is_left_to_reconcile(tmp_path):
    record, account_id, db_path = make_record(tmp_path)

    manager = FileManager(
        str(tmp_path / "bitbeard.db"),
        str(tmp_path / "media"),
    )
    file_relative = os.path.relpath(
        os.path.join(record.save_path, "file.bin"), str(tmp_path / "media")
    )
    manager.delete(file_relative.replace(os.sep, "/"))

    # The record and the counter stay; the periodic reconcile recomputes
    # the recorded size from what is left on disk.
    with closing(db.connect(db_path)) as connection:
        row = connection.execute(
            "SELECT size_bytes FROM downloads WHERE id = ?", (record.id,)
        ).fetchone()
    assert row is not None
    assert quota_used(db_path, account_id) == 1024


def test_rename_repoints_the_downloads_save_path(tmp_path):
    record, account_id, db_path = make_record(tmp_path)

    manager = FileManager(
        str(tmp_path / "bitbeard.db"),
        str(tmp_path / "media"),
    )
    parent = os.path.basename(os.path.dirname(record.save_path))
    leaf = os.path.basename(record.save_path)
    manager.rename(f"{parent}/{leaf}", "renamed")

    with closing(db.connect(db_path)) as connection:
        row = connection.execute(
            "SELECT save_path, size_bytes FROM downloads WHERE id = ?",
            (record.id,),
        ).fetchone()
    assert os.path.basename(row["save_path"]) == "renamed"
    assert row["size_bytes"] == 1024
    assert quota_used(db_path, account_id) == 1024


def test_move_repoints_the_downloads_save_path(tmp_path):
    record, account_id, db_path = make_record(tmp_path)
    os.makedirs(
        os.path.join(str(tmp_path / "media"), "Shows"), exist_ok=True
    )

    manager = FileManager(
        str(tmp_path / "bitbeard.db"),
        str(tmp_path / "media"),
    )
    parent = os.path.basename(os.path.dirname(record.save_path))
    leaf = os.path.basename(record.save_path)
    manager.move(f"{parent}/{leaf}", "Shows")

    with closing(db.connect(db_path)) as connection:
        row = connection.execute(
            "SELECT save_path FROM downloads WHERE id = ?", (record.id,)
        ).fetchone()
    assert os.path.basename(os.path.dirname(row["save_path"])) == "Shows"


# --- The manager HTTP surface ---------------------------------------------------


TOKEN = "service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeQbt:
    def torrents_info(self, status_filter=None):
        return []


def endpoint_client(tmp_path):
    config = make_config(tmp_path)
    os.makedirs(config.base_download_dir, exist_ok=True)
    app = create_app(config=config, qbt_client=FakeQbt())
    return TestClient(app), config


def write_file(base, *relative_parts, content=b"x"):
    path = os.path.join(base, *relative_parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def test_files_endpoint_lists_the_media_root(tmp_path):
    client, config = endpoint_client(tmp_path)
    write_file(config.base_download_dir, "Movies", "file.bin")

    with client:
        response = client.get("/files", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    names = {entry["name"]: entry for entry in body["entries"]}
    assert names["Movies"]["is_dir"] is True
    assert names["Movies"]["size_bytes"] == 0


def test_files_endpoint_lists_a_subdirectory(tmp_path):
    client, config = endpoint_client(tmp_path)
    write_file(config.base_download_dir, "Movies", "file.bin", content=b"abcd")

    with client:
        response = client.get("/files?path=Movies", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["path"] == "Movies"
    entry = response.json()["entries"][0]
    assert entry == {"name": "file.bin", "is_dir": False, "size_bytes": 4}


def test_files_endpoint_reports_unknown_paths_as_404(tmp_path):
    client, _ = endpoint_client(tmp_path)

    with client:
        response = client.get("/files?path=Missing", headers=AUTH)

    assert response.status_code == 404
    assert "No file or directory" in response.json()["detail"]


def test_delete_endpoint_hard_deletes(tmp_path):
    client, config = endpoint_client(tmp_path)
    write_file(config.base_download_dir, "Movies", "old", "file.bin")

    with client:
        response = client.post(
            "/files/delete",
            headers=AUTH,
            json={"path": "Movies/old"},
        )

    assert response.status_code == 200
    assert response.json() == {"deleted": "Movies/old"}
    assert not os.path.exists(
        os.path.join(config.base_download_dir, "Movies", "old")
    )


def test_delete_endpoint_refuses_traversal(tmp_path):
    client, config = endpoint_client(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with client:
        response = client.post(
            "/files/delete", headers=AUTH, json={"path": "../outside"}
        )

    assert response.status_code == 400
    assert outside.exists()


def test_delete_endpoint_refuses_protected_paths_with_409(tmp_path):
    config = make_config(tmp_path)
    save_path = os.path.join(config.base_download_dir, "Movies", "5")
    os.makedirs(save_path, exist_ok=True)

    class ActiveOnlyQbt:
        def torrents_info(self, status_filter=None):
            return [FakeTorrent("h1", save_path)]

    app = create_app(config=config, qbt_client=ActiveOnlyQbt())
    os.makedirs(config.base_download_dir, exist_ok=True)

    with TestClient(app) as client:
        response = client.post(
            "/files/delete", headers=AUTH, json={"path": "Movies/5"}
        )

    assert response.status_code == 409
    assert "active torrent" in response.json()["detail"]


def test_rename_and_move_endpoints(tmp_path):
    client, config = endpoint_client(tmp_path)
    write_file(config.base_download_dir, "Movies", "old.txt")
    os.makedirs(os.path.join(config.base_download_dir, "Shows"))

    with client:
        renamed = client.post(
            "/files/rename", headers=AUTH,
            json={"path": "Movies/old.txt", "new_name": "new.txt"},
        )
        moved = client.post(
            "/files/move", headers=AUTH,
            json={"path": "Movies/new.txt", "target_dir": "Shows"},
        )

    assert renamed.status_code == 200
    assert renamed.json() == {
        "renamed": "Movies/old.txt", "to": "Movies/new.txt",
    }
    assert moved.status_code == 200
    assert moved.json() == {
        "moved": "Movies/new.txt", "to": "Shows/new.txt",
    }
    assert os.path.exists(
        os.path.join(config.base_download_dir, "Shows", "new.txt")
    )


def test_rename_endpoint_refuses_taken_targets_and_bad_names(tmp_path):
    client, config = endpoint_client(tmp_path)
    write_file(config.base_download_dir, "Movies", "a.txt")
    write_file(config.base_download_dir, "Movies", "b.txt")

    with client:
        taken = client.post(
            "/files/rename", headers=AUTH,
            json={"path": "Movies/a.txt", "new_name": "b.txt"},
        )
        bad = client.post(
            "/files/rename", headers=AUTH,
            json={"path": "Movies/a.txt", "new_name": "../escape"},
        )

    assert taken.status_code == 400
    assert bad.status_code == 400


def test_file_endpoints_require_the_service_token(tmp_path):
    client, _ = endpoint_client(tmp_path)

    with client:
        assert client.get("/files").status_code == 401
        assert client.post(
            "/files/delete", json={"path": "Movies"}
        ).status_code == 401
