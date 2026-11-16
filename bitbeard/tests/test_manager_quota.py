"""Tests for Storage Quota accounting (manager.quota) and its wiring.

Each group maps to an acceptance criterion of ticket #5: the downloads
ownership record, the counter (increment at submit, adjust at completion,
decrement on delete), reconciliation, and the manager-side delete.
"""

import asyncio
import os
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from manager.config import Config
from manager.downloads import DownloadManager
from manager.main import create_app
from manager.quota import (
    ActiveTorrentError,
    DRIFT_WARN_BYTES,
    OutsideMediaRootError,
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
        reconcile_interval=0.0,  # the job is exercised directly
    )


def make_account(path, username="deckhand", role="user", quota_bytes=None):
    """Create an Account directly; Account creation arrives with #8."""
    with closing(db.connect(path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, quota_bytes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, "not-a-real-hash", role, quota_bytes, db.utc_now()),
        )
        connection.commit()
    return cursor.lastrowid


def quota_used(path, account_id):
    with closing(db.connect(path)) as connection:
        return connection.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (account_id,)
        ).fetchone()[0]


def get_row(path, download_id):
    with closing(db.connect(path)) as connection:
        row = connection.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
    return dict(row) if row else None


def make_ledger(config, qbt=None):
    db.initialize(config.db_path)
    return QuotaLedger(config.db_path, config.base_download_dir, qbt_client=qbt)


class FakeTorrent:
    def __init__(self, hash, name, size=0, status="downloading"):
        self.hash = hash
        self.name = name
        self.size = size
        self.progress = 0.5
        self.eta = 3600
        self.status = status
        self.save_path = None


class FakeQbt:
    """Scriptable stand-in: added torrents complete on the first poll."""

    def __init__(self, completed_size=0):
        self.torrents = []
        self.add_calls = []
        self.delete_calls = []
        self.add_error = None
        self._next_hash = 0
        self.completed_size = completed_size

    def torrents_add(self, urls=None, save_path=None):
        self.add_calls.append({"urls": urls, "save_path": save_path})
        if self.add_error is not None:
            raise self.add_error
        self._next_hash += 1
        torrent = FakeTorrent(
            f"hash{self._next_hash}", "Test Torrent", size=self.completed_size
        )
        torrent.save_path = save_path
        self.torrents.append(torrent)
        return "Ok."

    def torrents_info(self, status_filter=None):
        for torrent in self.torrents:
            if torrent.status == "downloading" and torrent.hash.startswith("hash"):
                torrent.status = "completed"
        if status_filter is None:
            return list(self.torrents)
        return [t for t in self.torrents if t.status == status_filter]

    def torrents_delete(self, torrent_hashes=None):
        self.delete_calls.append(torrent_hashes)
        self.torrents = [t for t in self.torrents if t.hash != torrent_hashes]


class CollectingAnnouncer:
    def __init__(self):
        self.submitted = []
        self.completed = []
        self.all_done_count = 0

    async def announce_submitted(self, torrent_name, category, actor=None,
                                 download_id=None):
        self.submitted.append((torrent_name, category, actor, download_id))

    async def announce_completed(self, torrent_name, category, actor=None,
                                 download_id=None):
        self.completed.append((torrent_name, category, actor, download_id))

    async def announce_all_done(self):
        self.all_done_count += 1


def make_manager(qbt=None, announcer=None, config=None, tmp_path=None):
    config = config or make_config(tmp_path)
    qbt = qbt if qbt is not None else FakeQbt()
    announcer = announcer if announcer is not None else CollectingAnnouncer()
    manager = DownloadManager(qbt, config, announcer, poll_interval=0.01)
    return manager, qbt, announcer, config


def test_an_audiobook_submission_lands_under_the_audiobook_subdirectory(
    tmp_path,
):
    # End to end for ticket #27: the ownership record is opened under the
    # Audiobook Category, the torrent is added into its per-Download
    # directory beneath the Media Root, and the directory is created.
    qbt = FakeQbt()
    config = make_config(tmp_path)
    ledger = make_ledger(config, qbt=qbt)
    make_account(config.db_path)
    manager = DownloadManager(
        qbt, config, CollectingAnnouncer(), poll_interval=0.01
    )

    record = ledger.open_download(
        account_id=None,
        discord_name="deckhand",
        title="Some Audiobook",
        category="Audiobook",
        size_bytes=0,
    )

    async def run():
        task = manager.submit(
            "magnet:?xt=urn:btih:abc",
            "Audiobook",
            actor="discord:deckhand",
            record=record,
        )
        await task

    asyncio.run(run())

    save_path = qbt.add_calls[0]["save_path"]
    assert save_path == os.path.join(
        config.base_download_dir, "Audiobook", str(record.id)
    )
    row = get_row(config.db_path, record.id)
    assert row["category"] == "Audiobook"
    assert row["save_path"] == save_path
    assert os.path.isdir(save_path)


# --- The ownership record ------------------------------------------------


def test_open_download_records_the_owning_account(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)

    record = ledger.open_download(
        account_id=account_id,
        discord_name=None,
        title="Some Movie",
        category="Movie",
        size_bytes=500,
    )

    row = get_row(config.db_path, record.id)
    assert row["account"] == account_id
    assert row["discord_name"] is None
    assert row["name"] == "Some Movie"
    assert row["category"] == "Movie"
    assert row["size_bytes"] == 500
    assert row["status"] == "in_flight"
    assert row["submitted_at"] is not None
    assert row["completed_at"] is None
    # The Download gets its own directory under the Category's, so its
    # files are deletable and accountable without touching neighbours.
    assert row["save_path"] == os.path.join(
        config.base_download_dir, "Movie", str(record.id)
    )
    assert record.save_path == row["save_path"]


def test_open_download_places_audiobook_downloads_in_its_subdirectory(
    tmp_path,
):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    make_account(config.db_path)

    record = ledger.open_download(
        account_id=None,
        discord_name="deckhand",
        title="Some Audiobook",
        category="Audiobook",
        size_bytes=500,
    )

    row = get_row(config.db_path, record.id)
    assert row["category"] == "Audiobook"
    assert row["save_path"] == os.path.join(
        config.base_download_dir, "Audiobook", str(record.id)
    )
    assert record.save_path == row["save_path"]


def test_open_download_charges_the_counter_at_submit(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)

    ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )

    # The In-flight size counts against the owner immediately.
    assert quota_used(config.db_path, account_id) == 500


def test_open_download_records_unlinked_submitters_by_discord_name(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)

    record = ledger.open_download(
        account_id=None,
        discord_name="discord:salty",
        title=None,
        category="Other",
        size_bytes=0,
    )

    row = get_row(config.db_path, record.id)
    assert row["account"] is None
    assert row["discord_name"] == "discord:salty"


def test_unlinked_submissions_do_not_charge_any_counter(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    bystander = make_account(config.db_path)

    ledger.open_download(
        account_id=None, discord_name="discord:salty", title=None,
        category="Other", size_bytes=999,
    )

    assert quota_used(config.db_path, bystander) == 0


def test_discovery_records_the_real_torrent_name(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name=None, title="Torznab Title",
        category="Movie", size_bytes=0,
    )

    record.discovered("qBittorrents Real Name")

    assert get_row(config.db_path, record.id)["name"] == "qBittorrents Real Name"


def test_completion_records_actual_size_and_adjusts_the_counter(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title="Some Movie",
        category="Movie", size_bytes=500,
    )

    record.completed("Some Movie", 400)

    row = get_row(config.db_path, record.id)
    assert row["status"] == "completed"
    assert row["size_bytes"] == 400
    assert row["completed_at"] is not None
    # The counter was charged 500 at submit and adjusted to the actual 400.
    assert quota_used(config.db_path, account_id) == 400


def test_completion_when_actual_matches_projected_keeps_the_counter(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=400,
    )

    record.completed("Some Movie", 400)

    assert quota_used(config.db_path, account_id) == 400


def test_completion_of_an_unlinked_download_is_recorded_without_a_counter(
    tmp_path,
):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name="discord:salty", title=None,
        category="Other", size_bytes=0,
    )

    record.completed("Some Movie", 400)

    assert get_row(config.db_path, record.id)["status"] == "completed"


def test_release_deletes_the_record_and_frees_the_counter(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )

    record.release()

    assert get_row(config.db_path, record.id) is None
    assert quota_used(config.db_path, account_id) == 0


def test_cancel_zeroes_the_record_and_frees_the_counter(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )

    record.cancelled()

    row = get_row(config.db_path, record.id)
    assert row["status"] == "cancelled"
    assert row["size_bytes"] == 0
    assert quota_used(config.db_path, account_id) == 0


# --- The user-facing delete operation (manager side) ----------------------


def write_files(base, *relative_parts, content=b"x"):
    path = os.path.join(base, *relative_parts)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "file.bin"), "wb") as handle:
        handle.write(content * 1024)
    return path


def test_delete_download_removes_files_record_and_quota(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=0,
    )
    write_files(config.base_download_dir, "Movie", str(record.id))

    deleted = ledger.delete_download(record.id)

    assert deleted["id"] == record.id
    assert not os.path.exists(record.save_path)
    assert get_row(config.db_path, record.id) is None
    assert quota_used(config.db_path, account_id) == 0


def test_delete_download_refuses_while_the_torrent_is_still_active(tmp_path):
    config = make_config(tmp_path)
    download_dir = write_files(config.base_download_dir, "Movie", "7")

    class ActiveQbt:
        def torrents_info(self):
            torrent = FakeTorrent("h", "Some Movie")
            torrent.save_path = download_dir
            return [torrent]

    ledger = make_ledger(config, qbt=ActiveQbt())
    db.initialize(config.db_path)
    make_account(config.db_path)
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO downloads (account, name, category, save_path,"
            " size_bytes, status, submitted_at)"
            " VALUES (1, 'Some Movie', 'Movie', ?, 0, 'in_flight', ?)",
            (download_dir, db.utc_now()),
        )
        connection.commit()

    with pytest.raises(ActiveTorrentError):
        ledger.delete_download(cursor.lastrowid)


def test_delete_download_refuses_paths_outside_the_media_root(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    db.initialize(config.db_path)
    make_account(config.db_path)
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO downloads (name, category, save_path, size_bytes,"
            " status, submitted_at)"
            " VALUES ('Evil', 'Other', ?, 0, 'completed', ?)",
            (str(tmp_path / "outside"), db.utc_now()),
        )
        connection.commit()

    with pytest.raises(OutsideMediaRootError):
        ledger.delete_download(cursor.lastrowid)


def test_delete_download_of_an_unknown_id_is_none(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)

    assert ledger.delete_download(999) is None


# --- The torrent lifecycle drives the record ------------------------------


def test_full_lifecycle_keeps_the_record_after_the_torrent_is_swept(tmp_path):
    """The completed torrent is deleted from qBittorrent; the row stays."""
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    qbt = FakeQbt(completed_size=4096)
    manager, _, announcer, _ = make_manager(
        qbt=qbt, config=config, tmp_path=tmp_path
    )

    record = ledger.open_download(
        account_id=account_id, discord_name=None, title="Some Movie",
        category="Movie", size_bytes=500,
    )

    async def run():
        await manager.submit(
            "magnet:?xt=urn:btih:abc", "Movie", actor="deckhand", record=record
        )

    asyncio.run(run())

    assert qbt.torrents == []  # the torrent was swept from qBittorrent...
    row = get_row(config.db_path, record.id)
    # ...but the ownership record stays, with the actual size.
    assert row["status"] == "completed"
    assert row["size_bytes"] == 4096
    assert row["save_path"] == record.save_path
    assert quota_used(config.db_path, account_id) == 4096
    # Lifecycle Events carry the download id.
    assert announcer.submitted == [
        ("Test Torrent", "Movie", "deckhand", record.id)
    ]
    assert announcer.completed == [
        ("Test Torrent", "Movie", "deckhand", record.id)
    ]


def test_the_torrent_is_added_into_the_records_own_directory(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    qbt = FakeQbt()
    manager, _, _, _ = make_manager(qbt=qbt, config=config, tmp_path=tmp_path)
    record = ledger.open_download(
        account_id=None, discord_name=None, title=None,
        category="Movie", size_bytes=0,
    )

    async def run():
        await manager.submit("magnet:?xt=urn:btih:abc", "Movie", record=record)

    asyncio.run(run())

    assert qbt.add_calls == [
        {"urls": "magnet:?xt=urn:btih:abc", "save_path": record.save_path}
    ]
    assert os.path.isdir(record.save_path)


def test_a_failed_add_releases_the_record(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    qbt = FakeQbt()
    qbt.add_error = RuntimeError("qbt down")
    manager, _, announcer, _ = make_manager(
        qbt=qbt, config=config, tmp_path=tmp_path
    )
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )

    async def run():
        await manager.submit("magnet:?xt=urn:btih:abc", "Movie", record=record)

    asyncio.run(run())

    assert get_row(config.db_path, record.id) is None
    assert quota_used(config.db_path, account_id) == 0
    assert announcer.completed == []


def test_cancel_all_cancels_the_records_of_swept_torrents(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    qbt = FakeQbt()
    manager, _, _, _ = make_manager(qbt=qbt, config=config, tmp_path=tmp_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )
    # Pretend the torrent is in qBittorrent, discovered by its monitor.
    torrent = FakeTorrent("hash1", "Test Torrent")
    torrent.save_path = record.save_path
    qbt.torrents.append(torrent)
    manager._records[torrent.hash] = record

    manager.cancel_all()

    row = get_row(config.db_path, record.id)
    assert row["status"] == "cancelled"
    assert quota_used(config.db_path, account_id) == 0


# --- Reconciliation --------------------------------------------------------


def test_reconcile_corrects_a_completed_records_size_from_disk(tmp_path, caplog):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=0,
    )
    record.completed("Some Movie", 3 * DRIFT_WARN_BYTES)
    # Out-of-band change: an admin removed part of the Download's files.
    write_files(config.base_download_dir, "Movie", str(record.id))

    summary = ledger.reconcile()

    row = get_row(config.db_path, record.id)
    assert row["size_bytes"] == 1024  # the one file that is left
    assert quota_used(config.db_path, account_id) == 1024
    assert summary["size_corrections"] == [
        {"id": record.id, "drift": 1024 - 3 * DRIFT_WARN_BYTES}
    ]
    assert "drifts" in caplog.text  # large drift is logged


def test_reconcile_marks_a_stuck_in_flight_download_completed_from_disk(
    tmp_path,
):
    """A manager restart leaves In-flight rows; files on disk settle them."""
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title="Some Movie",
        category="Movie", size_bytes=500,
    )
    write_files(config.base_download_dir, "Movie", str(record.id))

    summary = ledger.reconcile()

    row = get_row(config.db_path, record.id)
    assert row["status"] == "completed"
    assert row["size_bytes"] == 1024
    assert quota_used(config.db_path, account_id) == 1024
    assert summary["resolved_completed"] == [record.id]


def test_reconcile_releases_an_in_flight_download_that_never_materialized(
    tmp_path,
):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )

    summary = ledger.reconcile()

    assert get_row(config.db_path, record.id) is None
    assert quota_used(config.db_path, account_id) == 0
    assert summary["released"] == [record.id]


def test_reconcile_leaves_an_active_in_flight_download_alone(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=500,
    )
    os.makedirs(record.save_path, exist_ok=True)  # empty: nothing on disk yet

    class ActiveQbt:
        def torrents_info(self):
            torrent = FakeTorrent("h", "Test Torrent")
            torrent.save_path = record.save_path
            return [torrent]

    ledger._qbt = ActiveQbt()

    summary = ledger.reconcile()

    row = get_row(config.db_path, record.id)
    assert row["status"] == "in_flight"
    assert row["size_bytes"] == 500
    assert quota_used(config.db_path, account_id) == 500
    assert summary == {
        "size_corrections": [],
        "resolved_completed": [],
        "released": [],
        "counter_corrections": {},
    }


def test_reconcile_corrects_a_drifted_usage_counter(tmp_path, caplog):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=0,
    )
    record.completed("Some Movie", 1024)
    write_files(config.base_download_dir, "Movie", str(record.id))
    # Out-of-band change: the counter was clobbered beyond the warn threshold.
    db.set_quota_used(config.db_path, account_id, 2 * DRIFT_WARN_BYTES)

    summary = ledger.reconcile()

    assert quota_used(config.db_path, account_id) == 1024
    assert summary["counter_corrections"] == {
        account_id: 1024 - 2 * DRIFT_WARN_BYTES
    }
    assert "drifts" in caplog.text


def test_reconcile_resets_a_counter_whose_records_are_gone(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=1024,
    )
    record.release()
    db.set_quota_used(config.db_path, account_id, 1024)

    ledger.reconcile()

    assert quota_used(config.db_path, account_id) == 0


def test_reconcile_of_an_empty_store_is_quiet(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    make_account(config.db_path)

    summary = ledger.reconcile()

    assert summary == {
        "size_corrections": [],
        "resolved_completed": [],
        "released": [],
        "counter_corrections": {},
    }


# --- The manager HTTP surface ---------------------------------------------


class FakeManagerForApp:
    def __init__(self):
        self.submitted = []

    def submit(self, magnet_link, category, actor=None, record=None):
        self.submitted.append((magnet_link, category, actor, record))

    def progress(self):
        return []

    def cancel_all(self):
        return []


TOKEN = "service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_download_endpoint_opens_the_ownership_record_for_the_account(
    tmp_path,
):
    config = make_config(tmp_path)
    app = create_app(config=config, qbt_client=FakeQbt())
    manager = FakeManagerForApp()
    app.state.download_manager = manager
    db.initialize(config.db_path)
    account_id = make_account(config.db_path)

    with TestClient(app) as client:
        response = client.post(
            "/download",
            headers={
                **AUTH,
                "X-Bitbeard-Account-Id": str(account_id),
            },
            json={
                "magnet_link": "magnet:?xt=urn:btih:abc",
                "category": "Movie",
                "size_bytes": 2048,
                "title": None,
            },
        )

    assert response.status_code == 200
    download_id = response.json()["download_id"]
    row = get_row(config.db_path, download_id)
    assert row["account"] == account_id
    assert row["size_bytes"] == 2048
    assert row["status"] == "in_flight"
    magnet, category, actor, record = manager.submitted[0]
    assert record.id == download_id
    assert record.save_path == row["save_path"]
    assert quota_used(config.db_path, account_id) == 2048


def test_download_endpoint_records_unlinked_actors_by_discord_name(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config=config, qbt_client=FakeQbt())
    app.state.download_manager = FakeManagerForApp()
    db.initialize(config.db_path)

    with TestClient(app) as client:
        response = client.post(
            "/download",
            headers={**AUTH, "X-Bitbeard-Discord": "discord:salty"},
            json={
                "magnet_link": "magnet:?xt=urn:btih:abc",
                "category": "Movie",
                "size_bytes": 0,
                "title": None,
            },
        )

    download_id = response.json()["download_id"]
    row = get_row(config.db_path, download_id)
    assert row["account"] is None
    assert row["discord_name"] == "discord:salty"


def test_download_endpoint_defaults_to_unknown_size_and_title(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config=config, qbt_client=FakeQbt())
    app.state.download_manager = FakeManagerForApp()
    db.initialize(config.db_path)

    with TestClient(app) as client:
        response = client.post(
            "/download",
            headers=AUTH,
            json={"magnet_link": "magnet:?xt=urn:btih:abc", "category": "Movie"},
        )

    row = get_row(config.db_path, response.json()["download_id"])
    assert row["size_bytes"] == 0
    assert row["name"] == ""


def test_delete_endpoint_hard_deletes_a_finished_download(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config=config, qbt_client=FakeQbt())
    ledger = app.state.quota_ledger
    db.initialize(config.db_path)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movie", size_bytes=0,
    )
    record.completed("Some Movie", 1024)
    write_files(config.base_download_dir, "Movie", str(record.id))

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/delete",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(account_id)},
        )

    assert response.status_code == 200
    assert response.json() == {"deleted": record.id, "name": "Some Movie"}
    assert not os.path.exists(record.save_path)
    assert get_row(config.db_path, record.id) is None
    assert quota_used(config.db_path, account_id) == 0


def test_delete_endpoint_reports_unknown_and_active_downloads(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config=config, qbt_client=FakeQbt())
    ledger = app.state.quota_ledger
    db.initialize(config.db_path)
    record = ledger.open_download(
        account_id=None, discord_name="discord:salty", title=None,
        category="Movie", size_bytes=0,
    )
    # The torrent is still in qBittorrent under the record's save path.
    torrent = FakeTorrent("h1", "Test Torrent")
    torrent.save_path = record.save_path
    app.state.qbt_client.torrents.append(torrent)
    identity = {**AUTH, "X-Bitbeard-Discord": "discord:salty"}

    with TestClient(app) as client:
        missing = client.post("/media/999/delete", headers=identity)
        active = client.post(f"/media/{record.id}/delete", headers=identity)

    assert missing.status_code == 404
    assert active.status_code == 409
    assert "active torrent" in active.json()["detail"]
