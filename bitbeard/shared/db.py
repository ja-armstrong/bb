"""The one SQLite store for the stack.

Identity, Sessions, the Audit Ledger and Downloads all live in a single
database on the shared data volume so the API and the Download Manager write
to the same file. The API owns the schema and seeds the bootstrap Admin on
first boot; the manager reaches the same file through the same module.

Connections are short-lived and per caller. WAL plus a busy timeout lets the
two writers coexist without one blocking the other for long.
"""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

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


def find_account(path: str, username: str) -> sqlite3.Row | None:
    """Look up an Account by username, for credential checks at login."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()


def get_account(path: str, account_id: int) -> sqlite3.Row | None:
    """Look up an Account by id, for resolving the caller of a request."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE id = ?", (account_id,)
        ).fetchone()


def find_account_by_discord(
    path: str, discord_id: str
) -> sqlite3.Row | None:
    """Look up an Account by linked Discord ID.

    This is the Bot's attribution hop (ADR-0002): a bot-proxied call names
    the acting Discord user, and the API resolves it to the linked Account
    for the Audit Ledger. Unlinked users have no row here — their actions
    are attributed to ``discord:<name>`` instead.
    """
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()


def update_password(path: str, account_id: int, password: str) -> bool:
    """Replace an Account's password. Returns True when the Account exists.

    Callers decide which Sessions survive the change; this only owns the hash.
    """
    with closing(connect(path)) as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(password), account_id),
        )
        connection.commit()
    return cursor.rowcount > 0


def record_audit(path: str, actor: str, action: str, details: Any = None) -> None:
    """Append one Audit Ledger row. ``details`` is stored as JSON."""
    with closing(connect(path)) as connection:
        connection.execute(
            "INSERT INTO audit (ts, actor, action, details) VALUES (?, ?, ?, ?)",
            (
                utc_now(),
                actor,
                action,
                json.dumps(details) if details is not None else None,
            ),
        )
        connection.commit()


def _start_bound(start: str) -> str:
    """Make an inclusive lower bound for ``ts`` (string-compares ISO)."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
        return start + "T00:00:00"
    return start


def _end_bound(end: str) -> str:
    """Make an inclusive upper bound for ``ts`` (string-compares ISO).

    A date-only end must cover the whole day, and the rows carry a
    ``+00:00`` suffix after the seconds, so the bound needs to out-sort
    every timestamp within that day.
    """
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        return end + "T23:59:59.999999"
    return end


def query_audit(
    path: str,
    *,
    actor: str | None = None,
    action: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Read the Audit Ledger, filtered, newest first, with pagination.

    ``actor`` and ``action`` match exactly (the Admin Panel offers these as
    pickers); ``start``/``end`` are inclusive ISO dates or datetimes in UTC,
    the one clock the ledger writes. Returns the page plus the unpaginated
    total so a view can page through it.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if start:
        clauses.append("ts >= ?")
        params.append(_start_bound(start))
    if end:
        clauses.append("ts <= ?")
        params.append(_end_bound(end))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    with closing(connect(path)) as connection:
        total = connection.execute(
            f"SELECT COUNT(*) FROM audit{where}", params
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT id, ts, actor, action, details
            FROM audit{where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    entries = [
        {
            "id": row["id"],
            "ts": row["ts"],
            "actor": row["actor"],
            "action": row["action"],
            "details": json.loads(row["details"]) if row["details"] else None,
        }
        for row in rows
    ]
    return {"entries": entries, "total": total, "limit": limit, "offset": offset}
