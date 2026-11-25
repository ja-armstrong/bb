"""Tests for the download shelving: the incoming area, the auto-rename.

Covers the two library-hygiene guarantees: In-flight Downloads live
under ``Downloads/<Category>/<id>`` (out of the media server's Category
scans, so nothing half-written or numeric ever surfaces in a library),
and completion moves the slot into its Category under the torrent's
name — through both completion paths (the monitor's sweep and the
reconciler's from-disk resolution) and with every fallback that keeps
the numeric slot.
"""

import os
from contextlib import closing

from manager.config import Config
from manager.quota import QuotaLedger, safe_dir_name
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


def make_ledger(config):
    db.initialize(config.db_path)
    return QuotaLedger(config.db_path, config.base_download_dir, qbt_client=None)


def put_file(path, size=512):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "file.bin"), "wb") as handle:
        handle.write(b"\0" * size)


def get_row(config, download_id):
    with closing(db.connect(config.db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
    return dict(row) if row else None


def test_inflight_slots_live_under_the_incoming_area(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)

    record = ledger.open_download(
        account_id=None,
        discord_name="deckhand",
        title="A Film",
        category="Movies",
        size_bytes=1000,
    )

    assert record.save_path == os.path.join(
        config.base_download_dir, "Downloads", "Movies", str(record.id)
    )


def test_completion_moves_the_slot_into_the_category_under_its_name(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None,
        discord_name="deckhand",
        title="A Film",
        category="Movies",
        size_bytes=1000,
    )
    slot = record.save_path
    put_file(slot, size=700)

    record.completed("Letters to Juliet (2010) 1080p BrRip x264 -YIFY", 700)

    row = get_row(config, record.id)
    assert row["status"] == "completed"
    assert row["save_path"] == os.path.join(
        config.base_download_dir, "Movies",
        "Letters to Juliet (2010) 1080p BrRip x264 -YIFY",
    )
    assert os.path.isdir(row["save_path"])
    assert not os.path.exists(slot)  # the slot is gone


def test_dotted_release_names_are_spaced_for_the_media_server(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None,
        discord_name="deckhand",
        title="A Film",
        category="Movies",
        size_bytes=0,
    )
    put_file(record.save_path)

    record.completed("Letters.to.Juliet.2010.1080p.BrRip.x264-YIFY", 512)

    leaf = os.path.basename(get_row(config, record.id)["save_path"])
    assert leaf == "Letters to Juliet 2010 1080p BrRip x264-YIFY"


def test_taken_target_keeps_the_numeric_slot(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None,
        discord_name="deckhand",
        title="A Film",
        category="Movies",
        size_bytes=0,
    )
    put_file(record.save_path)
    # A stray directory already owns the target name.
    put_file(os.path.join(config.base_download_dir, "Movies", "Same Name"))

    record.completed("Same.Name", 512)

    row = get_row(config, record.id)
    assert row["status"] == "completed"
    assert row["save_path"] == record.save_path  # untouched
    assert os.path.isdir(record.save_path)
    assert os.path.isdir(
        os.path.join(config.base_download_dir, "Movies", "Same Name")
    )


def test_two_downloads_with_the_same_release_name_do_not_collide(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    first = ledger.open_download(
        account_id=None, discord_name="a", title="A Film",
        category="Movies", size_bytes=0,
    )
    second = ledger.open_download(
        account_id=None, discord_name="b", title="A Film",
        category="Movies", size_bytes=0,
    )
    put_file(first.save_path)
    put_file(second.save_path)

    first.completed("Same Release 2020", 512)
    second.completed("Same Release 2020", 512)

    first_row = get_row(config, first.id)
    second_row = get_row(config, second.id)
    assert first_row["save_path"].endswith("Same Release 2020")
    assert second_row["save_path"] == second.save_path  # keeps the slot
    assert second_row["status"] == "completed"


def test_unsafe_names_keep_the_slot(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name="deckhand", title="A Film",
        category="Movies", size_bytes=0,
    )
    put_file(record.save_path)

    record.completed("///", 512)  # sanitizes to nothing

    row = get_row(config, record.id)
    assert row["status"] == "completed"
    assert row["save_path"] == record.save_path


def test_long_names_are_truncated_not_rejected(tmp_path):
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name="deckhand", title="A Film",
        category="Movies", size_bytes=0,
    )
    put_file(record.save_path)

    record.completed("A" * 400, 512)

    leaf = os.path.basename(get_row(config, record.id)["save_path"])
    assert len(leaf) <= 150
    assert leaf.startswith("A" * 100)


def test_reconciler_resolved_completions_are_shelved_too(tmp_path):
    # The manager-restart path: an In-flight row whose torrent has left
    # qBittorrent is completed from disk — the shelving must happen on
    # that path as well, not only through the monitor's sweep.
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name="deckhand", title="A Film",
        category="Shows", size_bytes=1000,
    )
    put_file(record.save_path, size=800)

    summary = ledger.reconcile()

    assert record.id in summary["resolved_completed"]
    row = get_row(config, record.id)
    assert row["status"] == "completed"
    assert row["save_path"] == os.path.join(
        config.base_download_dir, "Shows", "A Film"
    )


def test_legacy_records_rename_in_place(tmp_path):
    # A record from before the incoming area: its slot already sits in
    # the Category directory; completion renames it there, not into the
    # incoming area.
    config = make_config(tmp_path)
    ledger = make_ledger(config)
    record = ledger.open_download(
        account_id=None, discord_name="deckhand", title="A Film",
        category="Movies", size_bytes=0,
    )
    legacy_slot = os.path.join(config.base_download_dir, "Movies", str(record.id))
    put_file(legacy_slot)
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            "UPDATE downloads SET save_path = ? WHERE id = ?",
            (legacy_slot, record.id),
        )
        connection.commit()

    record.completed("Legacy Release 2019", 512)

    row = get_row(config, record.id)
    assert row["save_path"] == os.path.join(
        config.base_download_dir, "Movies", "Legacy Release 2019"
    )


def test_safe_dir_name_rules():
    assert safe_dir_name("Letters.to.Juliet.2010") == "Letters to Juliet 2010"
    assert safe_dir_name("a/b\\c\x00d") == "abcd"
    assert safe_dir_name("  spaced  out  ") == "spaced out"
    assert safe_dir_name("---wrapped---") == "wrapped"
    assert safe_dir_name("...") is None
    assert safe_dir_name("") is None
    assert safe_dir_name(None) is None
    assert len(safe_dir_name("B" * 400)) == 150
