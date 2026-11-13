"""The one SQLite store for the stack.

Identity, Sessions, the Audit Ledger and Downloads all live in a single
database on the shared data volume so the API and the Download Manager write
to the same file. The API owns the schema and seeds the bootstrap Admin on
first boot; the manager reaches the same file through the same module.

Connections are short-lived and per caller. WAL plus a busy timeout lets the
two writers coexist without one blocking the other for long.
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    discord_id TEXT UNIQUE,
    quota_bytes INTEGER,
    quota_used_bytes INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account INTEGER REFERENCES users(id) ON DELETE SET NULL,
    discord_name TEXT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    save_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_flight',
    submitted_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor);
CREATE INDEX IF NOT EXISTS idx_downloads_account ON downloads(account);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
"""


def utc_now() -> str:
    """A timestamp in the one format the store writes."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    """Open a configured connection to the store at ``path``."""
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    """Create every table and index the stack needs. Idempotent."""
    connection.executescript(SCHEMA)
    connection.commit()


def seed_bootstrap_admin(
    connection: sqlite3.Connection,
    username: str,
    password: str,
) -> bool:
    """Seed the bootstrap Admin if no Account exists yet.

    Returns True when a row was inserted, False when the store already holds
    an Account (so a restart never resets a changed password).
    """
    account_count = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]
    if account_count:
        return False

    connection.execute(
        """
        INSERT INTO users (username, password_hash, role, created_at)
        VALUES (?, ?, 'admin', ?)
        """,
        (username, hash_password(password), utc_now()),
    )
    connection.commit()
    return True


def initialize(
    path: str,
    *,
    admin_user: str | None = None,
    admin_password: str | None = None,
) -> bool:
    """Create the store (and optionally the bootstrap Admin).

    Returns True when the bootstrap Admin was seeded.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    with closing(connect(path)) as connection:
        create_schema(connection)
        if admin_user is None or admin_password is None:
            return False
        return seed_bootstrap_admin(connection, admin_user, admin_password)
