"""Tests for the shared SQLite store."""
import sqlite3

from shared import db
from shared.security import verify_password


def table_names(path):
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def test_initialize_creates_every_table(tmp_path):
    path = str(tmp_path / "bitbeard.db")

    db.initialize(path)

    assert {"users", "sessions", "audit", "downloads"} <= table_names(path)


def test_initialize_creates_the_database_file_on_disk(tmp_path):
    path = tmp_path / "nested" / "bitbeard.db"

    db.initialize(str(path))

    assert path.exists()


def test_connection_uses_wal_and_foreign_keys(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)

    connection = db.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_initialize_seeds_bootstrap_admin(tmp_path):
    path = str(tmp_path / "bitbeard.db")

    seeded = db.initialize(path, admin_user="admin", admin_password="hunter2")

    assert seeded is True
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM users WHERE username = 'admin'"
        ).fetchone()
    assert row["role"] == "admin"
    assert row["disabled"] == 0
    assert row["quota_bytes"] is None  # Admins are quota-exempt
    assert verify_password("hunter2", row["password_hash"])
    assert not verify_password("wrong", row["password_hash"])


def test_initialize_without_admin_credentials_creates_schema_only(tmp_path):
    path = str(tmp_path / "bitbeard.db")

    seeded = db.initialize(path)

    assert seeded is False
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_bootstrap_admin_is_seeded_only_on_first_boot(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path, admin_user="admin", admin_password="first-password")

    seeded_again = db.initialize(
        path, admin_user="admin", admin_password="second-password"
    )

    assert seeded_again is False
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM users").fetchall()
    assert len(rows) == 1
    # The original password survives a restart; env changes do not reset it.
    assert verify_password("first-password", rows[0]["password_hash"])


# --- Link Codes (Discord linking, ADR-0002) ---------------------------------


def make_account(path, username="deckhand", discord_id=None):
    db.initialize(path)
    with db.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO users (username, password_hash, role, discord_id, created_at)
            VALUES (?, ?, 'user', ?, ?)
            """,
            (username, "x", discord_id, db.utc_now()),
        )
        connection.commit()
    return db.find_account(path, username)


def test_create_link_code_stores_a_pending_binding(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    account = make_account(path)

    code = db.create_link_code(path, account["id"], "987654321")

    pending = db.pending_link_code(path, account["id"])
    assert pending["code"] == code
    assert pending["discord_id"] == "987654321"


def test_a_new_link_code_retires_the_previous_pending_one(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    account = make_account(path)

    first = db.create_link_code(path, account["id"], "987654321")
    second = db.create_link_code(path, account["id"], "987654321")

    assert first != second
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM link_codes WHERE account_id = ?",
            (account["id"],),
        ).fetchone()[0]
    assert count == 1
    assert db.pending_link_code(path, account["id"])["code"] == second


def test_consuming_a_pending_link_binds_the_account_one_time(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    account = make_account(path)
    db.create_link_code(path, account["id"], "987654321")

    linked = db.consume_pending_link(path, "987654321")

    assert linked is not None
    assert linked["id"] == account["id"]
    assert linked["discord_id"] == "987654321"

    # One-time use: the code is spent.
    assert db.consume_pending_link(path, "987654321") is None
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM link_codes"
        ).fetchone()[0]
    assert count == 0


def test_consuming_a_link_for_an_id_another_account_holds_refuses_and_spends(
    tmp_path,
):
    path = str(tmp_path / "bitbeard.db")
    other = make_account(path, username="bosun", discord_id="987654321")
    account = make_account(path, username="deckhand")
    db.create_link_code(path, account["id"], "987654321")

    linked = db.consume_pending_link(path, "987654321")

    assert linked is None
    assert db.find_account(path, "bosun")["id"] == other["id"]
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM link_codes"
        ).fetchone()[0]
    assert count == 0


def test_a_link_code_for_an_unclaimed_discord_id_does_nothing(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    account = make_account(path)
    db.create_link_code(path, account["id"], "987654321")

    assert db.consume_pending_link(path, "111222333") is None
    # The pending code for its own ID is untouched.
    assert db.pending_link_code(path, account["id"]) is not None
