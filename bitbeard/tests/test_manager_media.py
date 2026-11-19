"""Tests for the manager's My Media operations (ticket #6).

Each group maps to an acceptance criterion: the Actor's own Download list
with live progress, delete/rename/move on completed Downloads with the
Protected Path and Media Root guards, Download-level cancel, and the
manager-side ownership refusals behind the API's boundary check.
"""

import os
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from manager.config import Config
from manager.main import create_app
from manager.quota import (
    ActiveTorrentError,
    NotCompletedError,
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


def make_account(path, username="deckhand", role="user"):
    with closing(db.connect(path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, "not-a-real-hash", role, db.utc_now()),
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


def completed_download(ledger, config, account_id=None, name="Some Movies",
                       category="Movies", size_bytes=1024):
    """A completed Download record with files actually on disk."""
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=name,
        category=category, size_bytes=size_bytes,
    )
    record.completed(name, size_bytes)
    write_files(config.base_download_dir, category, str(record.id))
    return record


def write_files(base, *relative_parts, content=b"x"):
    path = os.path.join(base, *relative_parts)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "file.bin"), "wb") as handle:
        handle.write(content * 1024)
    return path


class FakeTorrent:
    def __init__(self, hash, name, size=0):
        self.hash = hash
        self.name = name
        self.size = size
        self.progress = 0.5
        self.eta = 3600
        self.save_path = None


class FakeQbt:
    """Scriptable stand-in; torrents carry their save path."""

    def __init__(self):
        self.torrents = []
        self.delete_calls = []

    def torrents_info(self, status_filter=None):
        return list(self.torrents)

    def torrents_delete(self, torrent_hashes=None):
        self.delete_calls.append(torrent_hashes)
        self.torrents = [t for t in self.torrents if t.hash != torrent_hashes]


class FakeManagerForMedia:
    """Scriptable stand-in: reports the torrents swept per save path."""

    def __init__(self, names=None):
        self.cancelled_paths = []
        self.names = names or {}

    def cancel_for(self, save_path):
        self.cancelled_paths.append(save_path)
        return self.names.get(save_path, [])


TOKEN = "service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- GET /media: the Actor's own Downloads ---------------------------------


def test_list_downloads_scopes_to_the_owning_account(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    hers = make_account(config.db_path, "matey")
    his = make_account(config.db_path, "deckhand")
    completed_download(ledger, config, account_id=hers, name="Her Movies")
    completed_download(ledger, config, account_id=his, name="His Movies")

    hers_media = ledger.list_downloads(account_id=hers)

    assert [entry["name"] for entry in hers_media] == ["Her Movies"]


def test_list_downloads_scopes_unlinked_discord_actors_by_name(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name="discord:salty", title="Salty Movies",
        category="Other", size_bytes=0,
    )
    record.completed("Salty Movies", 512)
    bystander = make_account(config.db_path)

    media = ledger.list_downloads(discord_name="discord:salty")

    assert [entry["id"] for entry in media] == [record.id]
    # An Account query does not see the unlinked Actor's rows.
    assert ledger.list_downloads(account_id=bystander) == []


def test_list_downloads_attaches_live_progress_to_in_flight_rows(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    ledger._qbt = FakeQbt()
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movies", size_bytes=500,
    )
    torrent = FakeTorrent("h1", "Test Torrent")
    torrent.save_path = record.save_path
    ledger._qbt.torrents.append(torrent)

    media = ledger.list_downloads(account_id=account_id)

    assert len(media) == 1
    assert media[0]["status"] == "in_flight"
    assert media[0]["progress"] == "50.00%"
    assert media[0]["eta"] == "1h:0m:0s"
    # The in-flight row is still just that: not yet completed.
    assert get_row(config.db_path, record.id)["status"] == "in_flight"


def test_list_downloads_reports_completed_rows_without_progress(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    completed_download(ledger, config, account_id=account_id)

    media = ledger.list_downloads(account_id=account_id)

    assert len(media) == 1
    assert media[0]["status"] == "completed"
    assert "progress" not in media[0]


def test_list_downloads_omits_cancelled_rows(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movies", size_bytes=500,
    )
    record.cancelled()

    assert ledger.list_downloads(account_id=account_id) == []


def test_list_downloads_without_an_owner_lists_nothing(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    completed_download(ledger, config, account_id=make_account(config.db_path))

    assert ledger.list_downloads() == []


# --- Rename ------------------------------------------------------------------


def test_rename_download_updates_the_record_and_the_disk(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    used_before = quota_used(config.db_path, account_id)

    updated = ledger.rename_download(record.id, "Pirates Yarn")

    assert updated["name"] == "Pirates Yarn"
    new_path = os.path.join(
        config.base_download_dir, "Movies", "Pirates Yarn"
    )
    assert updated["save_path"] == new_path
    assert os.path.exists(new_path)
    assert not os.path.exists(record.save_path)
    # Renaming does not change what the Download costs.
    assert quota_used(config.db_path, account_id) == used_before


def test_rename_download_refuses_while_the_torrent_is_still_active(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    torrent = FakeTorrent("h1", "Some Movies")
    torrent.save_path = record.save_path
    ledger._qbt = FakeQbt()
    ledger._qbt.torrents.append(torrent)

    with pytest.raises(ActiveTorrentError):
        ledger.rename_download(record.id, "Pirates Yarn")


def test_rename_download_refuses_in_flight_records(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name=None, title=None,
        category="Movies", size_bytes=0,
    )

    with pytest.raises(NotCompletedError):
        ledger.rename_download(record.id, "Pirates Yarn")


def test_rename_download_refuses_a_taken_target(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    # Another Download's directory is already sitting at the target.
    write_files(config.base_download_dir, "Movies", "Pirates Yarn")

    with pytest.raises(PathTakenError):
        ledger.rename_download(record.id, "Pirates Yarn")


def test_rename_download_refuses_another_records_save_path(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    # Another Download record already claims the target save path.
    other = ledger.open_download(
        account_id=account_id, discord_name=None, title="Occupied",
        category="Movies", size_bytes=0,
    )

    with pytest.raises(PathTakenError):
        ledger.rename_download(record.id, str(other.id))


def test_rename_download_refuses_paths_outside_the_media_root(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)

    with pytest.raises(OutsideMediaRootError):
        ledger.rename_download(record.id, "../../outside")

    assert os.path.exists(record.save_path)  # nothing moved


def test_rename_download_of_an_unknown_id_is_none(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)

    assert ledger.rename_download(999, "Whatever") is None


# --- Move to another Category ------------------------------------------------


def test_move_download_updates_record_disk_and_keeps_quota(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    used_before = quota_used(config.db_path, account_id)

    updated = ledger.move_download(record.id, "Shows")

    assert updated["category"] == "Shows"
    new_path = os.path.join(
        config.base_download_dir, "Shows", str(record.id)
    )
    assert updated["save_path"] == new_path
    assert os.path.exists(new_path)
    assert not os.path.exists(record.save_path)
    # Moving is free: the same bytes, a new shelf.
    assert quota_used(config.db_path, account_id) == used_before


def test_move_download_refuses_while_the_torrent_is_still_active(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    torrent = FakeTorrent("h1", "Some Movies")
    torrent.save_path = record.save_path
    ledger._qbt = FakeQbt()
    ledger._qbt.torrents.append(torrent)

    with pytest.raises(ActiveTorrentError):
        ledger.move_download(record.id, "Shows")


def test_move_download_refuses_paths_outside_the_media_root(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)

    with pytest.raises(OutsideMediaRootError):
        ledger.move_download(record.id, "../../outside")

    assert os.path.exists(record.save_path)


# --- The manager HTTP surface -------------------------------------------------


def make_app(tmp_path, qbt=None, manager=None):
    config = make_config(tmp_path)
    db.initialize(config.db_path)
    app = create_app(config=config, qbt_client=qbt or FakeQbt())
    if manager is not None:
        app.state.download_manager = manager
    return app, config


def test_media_list_endpoint_scopes_by_the_trusted_headers(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    hers = make_account(config.db_path, "matey")
    his = make_account(config.db_path, "deckhand")
    completed_download(ledger, config, account_id=hers, name="Her Movies")
    completed_download(ledger, config, account_id=his, name="His Movies")

    with TestClient(app) as client:
        response = client.get(
            "/media", headers={**AUTH, "X-Bitbeard-Account-Id": str(hers)}
        )

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["downloads"]]
    assert names == ["Her Movies"]


def test_media_list_endpoint_refuses_unattributed_calls(tmp_path):
    app, _ = make_app(tmp_path)

    with TestClient(app) as client:
        response = client.get("/media", headers=AUTH)

    assert response.status_code == 403


def test_media_list_endpoint_scopes_unlinked_discord_actors(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    record = ledger.open_download(
        account_id=None, discord_name="discord:salty", title="Salty Movies",
        category="Other", size_bytes=0,
    )
    record.completed("Salty Movies", 512)

    with TestClient(app) as client:
        response = client.get(
            "/media", headers={**AUTH, "X-Bitbeard-Discord": "discord:salty"}
        )

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["downloads"]] == [record.id]


def test_delete_endpoint_refuses_another_actors_download(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    hers = make_account(config.db_path, "matey")
    record = completed_download(ledger, config, account_id=hers)

    with TestClient(app) as client:
        stranger = client.post(
            f"/media/{record.id}/delete",
            headers={**AUTH, "X-Bitbeard-Account-Id": "424242"},
        )
        owner = client.post(
            f"/media/{record.id}/delete",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(hers)},
        )

    assert stranger.status_code == 403
    assert owner.status_code == 200
    assert get_row(config.db_path, record.id) is None


def test_rename_endpoint_updates_record_and_disk(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/rename",
            headers={
                **AUTH,
                "X-Bitbeard-Account-Id": str(account_id),
            },
            json={"name": "Pirates Yarn"},
        )

    assert response.status_code == 200
    assert response.json() == {"renamed": record.id, "name": "Pirates Yarn"}
    row = get_row(config.db_path, record.id)
    assert row["name"] == "Pirates Yarn"
    assert row["save_path"].endswith(os.path.join("Movies", "Pirates Yarn"))
    assert os.path.exists(row["save_path"])


def test_rename_endpoint_refuses_path_traversal_names(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/rename",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(account_id)},
            json={"name": "../escape"},
        )

    assert response.status_code == 400
    assert os.path.exists(record.save_path)  # nothing moved


def test_move_endpoint_refuses_unknown_categories(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/move",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(account_id)},
            json={"category": "../Evil"},
        )

    assert response.status_code == 400


def test_rename_endpoint_refuses_an_active_torrent(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    torrent = FakeTorrent("h1", "Some Movies")
    torrent.save_path = record.save_path
    app.state.qbt_client.torrents.append(torrent)

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/rename",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(account_id)},
            json={"name": "Pirates Yarn"},
        )

    assert response.status_code == 409
    assert "active torrent" in response.json()["detail"]


def test_cancel_endpoint_sweeps_only_that_download(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    account_id = make_account(config.db_path)
    record = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movies", size_bytes=500,
    )
    other = ledger.open_download(
        account_id=account_id, discord_name=None, title=None,
        category="Movies", size_bytes=300,
    )
    manager = FakeManagerForMedia(names={record.save_path: ["Test Torrent"]})
    app.state.download_manager = manager

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/cancel",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(account_id)},
        )

    assert response.status_code == 200
    assert response.json() == {"cancelled": ["Test Torrent"]}
    # Only the target's save path was handed to the lifecycle...
    assert manager.cancelled_paths == [record.save_path]
    # ...and only its record was settled: cancelled, quota freed.
    assert get_row(config.db_path, record.id)["status"] == "cancelled"
    assert get_row(config.db_path, other.id)["status"] == "in_flight"
    assert quota_used(config.db_path, account_id) == 300


def test_cancel_endpoint_refuses_a_download_that_is_not_in_flight(tmp_path):
    app, config = make_app(tmp_path)
    ledger = app.state.quota_ledger
    account_id = make_account(config.db_path)
    record = completed_download(ledger, config, account_id=account_id)
    app.state.download_manager = FakeManagerForMedia()

    with TestClient(app) as client:
        response = client.post(
            f"/media/{record.id}/cancel",
            headers={**AUTH, "X-Bitbeard-Account-Id": str(account_id)},
        )

    assert response.status_code == 409
