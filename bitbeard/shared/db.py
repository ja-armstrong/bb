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
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .security import hash_password

#: Sentinel in ``users.quota_bytes`` meaning the Account has no cap at all.
#: ``NULL`` means "use the API's global default"; any other value is the cap.
QUOTA_UNLIMITED = -1

#: Link Code alphabet: no easily-confused glyphs (0/O, 1/I/L), so a code
#: read off the Webui can be typed back without ambiguity.
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

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

CREATE TABLE IF NOT EXISTS link_codes (
    code TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    discord_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registration_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    note TEXT,
    source_ip TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_states (
    name TEXT PRIMARY KEY,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor);
CREATE INDEX IF NOT EXISTS idx_downloads_account ON downloads(account);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_link_codes_discord ON link_codes(discord_id);
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


# --- Account management (Admin Panel) ---------------------------------------
#
# Accounts are created and managed by an Admin from the Admin Panel (the
# bootstrap Admin arrives from env instead). These helpers own the row
# work; the API decides what is allowed and writes the ledger rows.


def create_account(
    path: str,
    *,
    username: str,
    password: str,
    role: str,
    quota_bytes: int | None = None,
) -> int | None:
    """Create an Account with a hashed password; returns its id.

    ``quota_bytes`` is the Storage Quota override — ``None`` for the
    global default, ``QUOTA_UNLIMITED`` for no cap. A duplicate username
    returns None: the UNIQUE constraint is the guard of record, so a
    race between the caller's pre-check and the insert cannot double-
    create.
    """
    with closing(connect(path)) as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (username, password_hash, role,
                                   quota_bytes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, hash_password(password), role, quota_bytes, utc_now()),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            return None
    return cursor.lastrowid


def list_accounts(path: str) -> list[sqlite3.Row]:
    """Every Account, oldest first, for the Admin Panel's Users page."""
    with closing(connect(path)) as connection:
        return connection.execute("SELECT * FROM users ORDER BY id").fetchall()


def count_enabled_admins(path: str) -> int:
    """How many enabled Admin Accounts exist — the last-admin guard."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
        ).fetchone()[0]


def set_disabled(path: str, account_id: int, disabled: bool) -> bool:
    """Enable or disable an Account. Returns True when the Account exists.

    Callers decide which Sessions survive; disabling an Account already
    refuses every request at the API boundary.
    """
    with closing(connect(path)) as connection:
        cursor = connection.execute(
            "UPDATE users SET disabled = ? WHERE id = ?",
            (int(disabled), account_id),
        )
        connection.commit()
    return cursor.rowcount > 0


def revoke_sessions_for_account(
    path: str, account_id: int, *, keep_token: str | None = None
) -> int:
    """Revoke every Session of an Account; returns how many died.

    ``keep_token`` spares one Session — the self-service password change
    keeps the browser that made the change. Admin actions (reset,
    disable, revoke) leave nothing alive.
    """
    with closing(connect(path)) as connection:
        if keep_token is None:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE account_id = ?", (account_id,)
            )
        else:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE account_id = ? AND token != ?",
                (account_id, keep_token),
            )
        connection.commit()
    return cursor.rowcount


# --- Registration Requests (public submissions) -----------------------------
#
# Pending asks from an anonymous visitor for an Account, submitted through
# the Webui's public landing page. A row is pending state only: the Admin's
# decision (approve creates the Account, deny discards) deletes it, and the
# decision lives on in the Audit Ledger. The password is hashed here with
# the same mechanism Accounts use — plaintext is never stored.

def create_registration_request(
    path: str,
    *,
    username: str,
    password: str,
    note: str | None,
    source_ip: str,
) -> int | None:
    """Store a pending Registration Request; returns its id.

    The password is hashed here, at the store boundary, through the same
    ``shared.security`` mechanism Accounts use, so the plaintext the
    visitor typed never lands in the store. A duplicate username returns
    None: the UNIQUE constraint is the guard of record, mirroring
    ``create_account`` so a race cannot double-file a request.
    """
    with closing(connect(path)) as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO registration_requests
                    (username, password_hash, note, source_ip, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, hash_password(password), note, source_ip, utc_now()),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            return None
    return cursor.lastrowid


def pending_registration_request(path: str, username: str) -> sqlite3.Row | None:
    """The pending Registration Request for a username, for conflict checks.

    A pending request and an existing Account both block the same
    username at submission; callers name which situation they hit.
    """
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM registration_requests WHERE username = ?",
            (username,),
        ).fetchone()


def list_registration_requests(path: str) -> list[sqlite3.Row]:
    """Every pending Registration Request, oldest first, for the Admin
    Panel's review queue."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM registration_requests ORDER BY id"
        ).fetchall()


def get_registration_request(
    path: str, request_id: int
) -> sqlite3.Row | None:
    """Look up one pending Registration Request by id, for the review
    actions."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM registration_requests WHERE id = ?", (request_id,)
        ).fetchone()


def create_account_from_hash(
    path: str,
    *,
    username: str,
    password_hash: str,
    role: str,
    quota_bytes: int | None = None,
) -> int | None:
    """Create an Account from an already-hashed password; returns its id.

    Mirrors ``create_account`` — the UNIQUE constraint is the guard of
    record, so a race between the caller's pre-check and the insert
    cannot double-create — but takes the stored hash instead of
    plaintext. This is how an approved Registration Request becomes an
    Account: the hash the visitor's password produced at submission is
    carried over untouched, so the credentials they chose log in
    verbatim with no reset ceremony.
    """
    with closing(connect(path)) as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (username, password_hash, role,
                                   quota_bytes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, password_hash, role, quota_bytes, utc_now()),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            return None
    return cursor.lastrowid


def delete_registration_request(
    path: str, request_id: int
) -> sqlite3.Row | None:
    """Drop a pending Registration Request; returns the row so the caller
    can audit what it refused."""
    with closing(connect(path)) as connection:
        row = connection.execute(
            "SELECT * FROM registration_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "DELETE FROM registration_requests WHERE id = ?", (request_id,)
        )
        connection.commit()
    return row


# --- Link Codes (Discord linking, ADR-0002) --------------------------------
#
# A one-time Link Code, generated in the Webui, binds an Account to a
# Discord ID. The code alone never links anything: it sits pending until
# the Bot relays an interaction from the claimed Discord ID — the API
# consumes it on the next bot-proxied call — so the binding is proven by
# the Discord user actually showing up, not just by whoever holds the code.


def create_link_code(path: str, account_id: int, discord_id: str) -> str:
    """Issue a one-time Link Code binding an Account to a Discord ID.

    Generating a new code retires the Account's previous pending one, so
    exactly one code is ever live per Account. Returns the code for the
    Webui to show once; the store holds the pending binding.
    """
    code = "".join(
        secrets.choice(LINK_CODE_ALPHABET) for _ in range(8)
    )
    with closing(connect(path)) as connection:
        connection.execute(
            "DELETE FROM link_codes WHERE account_id = ?", (account_id,)
        )
        connection.execute(
            """
            INSERT INTO link_codes (code, account_id, discord_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (code, account_id, discord_id, utc_now()),
        )
        connection.commit()
    return code


def pending_link_code(path: str, account_id: int) -> sqlite3.Row | None:
    """The Account's live Link Code, for the Webui's pending display."""
    with closing(connect(path)) as connection:
        return connection.execute(
            """
            SELECT code, discord_id, created_at FROM link_codes
            WHERE account_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (account_id,),
        ).fetchone()


def consume_pending_link(path: str, discord_id: str) -> sqlite3.Row | None:
    """Verify and spend the pending Link Code for this Discord ID.

    Called on every bot-proxied request with the acting Discord user's
    ID: when a pending code claims that ID, the Account is bound to it
    and the Account row is returned. One-time use — the code is spent
    whether or not the bind succeeds — and refused when the Discord ID
    is already linked to another Account (the code is still spent, so a
    stale claim cannot linger).
    """
    with closing(connect(path)) as connection:
        row = connection.execute(
            """
            SELECT code, account_id FROM link_codes
            WHERE discord_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (discord_id,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "DELETE FROM link_codes WHERE code = ?", (row["code"],)
        )
        clash = connection.execute(
            "SELECT id FROM users WHERE discord_id = ? AND id != ?",
            (discord_id, row["account_id"]),
        ).fetchone()
        if clash is not None:
            connection.commit()
            return None
        connection.execute(
            "UPDATE users SET discord_id = ? WHERE id = ?",
            (discord_id, row["account_id"]),
        )
        connection.commit()
    return get_account(path, row["account_id"])


# --- Subsystem disable state (Admin Panel) -----------------------------------
#
# Whether an Optional Subsystem is disabled survives restarts of the API in
# this one-row-per-Subsystem table: a container can be stopped and gone from
# a live docker listing, but "disabled" is a decision the Admin made, not a
# container state. Only Optional Subsystems (the Bot today) ever get a row.

def service_disabled(path: str, name: str) -> bool:
    """Whether a Subsystem is recorded as disabled by the Admin Panel."""
    with closing(connect(path)) as connection:
        row = connection.execute(
            "SELECT disabled FROM service_states WHERE name = ?", (name,)
        ).fetchone()
    return bool(row) and bool(row["disabled"])


def set_service_disabled(path: str, name: str, disabled: bool) -> None:
    """Record (or clear) a Subsystem's disabled state."""
    with closing(connect(path)) as connection:
        connection.execute(
            """
            INSERT INTO service_states (name, disabled) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET disabled = excluded.disabled
            """,
            (name, int(disabled)),
        )
        connection.commit()


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


# --- Downloads (ownership records) ----------------------------------------
#
# The ``downloads`` row is the persistent ownership record of a Download:
# qBittorrent state is never consulted for ownership. The Download Manager
# writes the lifecycle (submit -> discover -> complete/cancel/release); the
# API reads the same rows to pre-check Storage Quotas and answer /me/quota.


def insert_download(
    path: str,
    *,
    account: int | None,
    discord_name: str | None,
    name: str,
    category: str,
    save_path: str,
    size_bytes: int,
) -> int:
    """Insert the ownership record of a just-submitted Download.

    The row starts In-flight with the projected size (0 when unknown, e.g.
    a magnet submitted without a Torznab result at hand). Returns the row
    id the manager keys the torrent lifecycle on.
    """
    with closing(connect(path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO downloads
                (account, discord_name, name, category, save_path,
                 size_bytes, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, 'in_flight', ?)
            """,
            (account, discord_name, name, category, save_path, size_bytes, utc_now()),
        )
        connection.commit()
    return cursor.lastrowid


def get_download(path: str, download_id: int) -> sqlite3.Row | None:
    """Look up one Download record by id."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()


def set_download_save_path(path: str, download_id: int, save_path: str) -> None:
    """Point a Download record at its per-Download directory.

    The save path is carved out of the Media Root as
    ``<Media Root>/<Category>/<download id>`` once the row id exists, so a
    Download's files are deletable and accountable without touching its
    neighbours.
    """
    with closing(connect(path)) as connection:
        connection.execute(
            "UPDATE downloads SET save_path = ? WHERE id = ?",
            (save_path, download_id),
        )
        connection.commit()


def update_download_name(path: str, download_id: int, name: str) -> None:
    """Record the torrent's real name once qBittorrent knows it.

    At submission only the Torznab title (if any) is known; qBittorrent
    assigns the authoritative name when metadata arrives.
    """
    with closing(connect(path)) as connection:
        connection.execute(
            "UPDATE downloads SET name = ? WHERE id = ?", (name, download_id)
        )
        connection.commit()


def set_download_size(path: str, download_id: int, size_bytes: int) -> None:
    """Correct a Download's recorded size without touching status."""
    with closing(connect(path)) as connection:
        connection.execute(
            "UPDATE downloads SET size_bytes = ? WHERE id = ?",
            (size_bytes, download_id),
        )
        connection.commit()


def update_download_category(path: str, download_id: int, category: str) -> None:
    """Point a Download record at its new Category after a move."""
    with closing(connect(path)) as connection:
        connection.execute(
            "UPDATE downloads SET category = ? WHERE id = ?",
            (category, download_id),
        )
        connection.commit()


def complete_download(
    path: str, download_id: int, *, name: str, size_bytes: int
) -> None:
    """Mark a Download completed: actual size, completion timestamp.

    The row is kept after the torrent is deleted from qBittorrent — the
    files remain and stay owned (the spec's decision of record).
    """
    with closing(connect(path)) as connection:
        connection.execute(
            """
            UPDATE downloads
            SET name = ?, size_bytes = ?, status = 'completed', completed_at = ?
            WHERE id = ?
            """,
            (name, size_bytes, utc_now(), download_id),
        )
        connection.commit()


def cancel_download(path: str, download_id: int) -> None:
    """Mark a Download cancelled: it no longer counts against its owner.

    Partial files the torrent already wrote are left on disk (the Admin
    file manager owns Media Root cleanup) but carry no quota.
    """
    with closing(connect(path)) as connection:
        connection.execute(
            """
            UPDATE downloads
            SET status = 'cancelled', size_bytes = 0, completed_at = ?
            WHERE id = ?
            """,
            (utc_now(), download_id),
        )
        connection.commit()


def delete_download_row(path: str, download_id: int) -> sqlite3.Row | None:
    """Drop a Download record; returns the row so callers can settle quota."""
    with closing(connect(path)) as connection:
        row = connection.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        connection.commit()
    return row


def all_downloads(path: str) -> list[sqlite3.Row]:
    """Every Download record, for reconciliation."""
    with closing(connect(path)) as connection:
        return connection.execute("SELECT * FROM downloads").fetchall()


def downloads_for_owner(
    path: str,
    *,
    account_id: int | None = None,
    discord_name: str | None = None,
) -> list[sqlite3.Row]:
    """One Actor's Download records, newest first.

    ``account_id`` names a linked Account; ``discord_name`` names the
    ``discord:<name>`` Actor of an unlinked submitter. The Account filter
    wins when both arrive, matching how submissions set ownership. With
    neither, there is no Actor to scope to: nothing is returned.
    """
    with closing(connect(path)) as connection:
        if account_id is not None:
            return connection.execute(
                "SELECT * FROM downloads WHERE account = ? ORDER BY id DESC",
                (account_id,),
            ).fetchall()
        if discord_name is not None:
            return connection.execute(
                "SELECT * FROM downloads WHERE account IS NULL"
                " AND discord_name = ? ORDER BY id DESC",
                (discord_name,),
            ).fetchall()
        return []


def get_download_by_save_path(path: str, save_path: str) -> sqlite3.Row | None:
    """Find the Download record parked at a save path, for rename/move."""
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM downloads WHERE save_path = ?", (save_path,)
        ).fetchone()


def sum_download_sizes(path: str, account_id: int) -> int:
    """One Account's recorded Download bytes: on-disk plus In-flight.

    Completed rows carry the actual on-disk size, In-flight rows the
    projected one, cancelled rows zero — so this sum is the projected
    usage the submission pre-check compares against the cap.
    """
    with closing(connect(path)) as connection:
        return connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM downloads WHERE account = ?",
            (account_id,),
        ).fetchone()[0]


def download_usage_by_account(path: str) -> dict[int, int]:
    """Recorded Download bytes per Account id, for reconciliation."""
    with closing(connect(path)) as connection:
        rows = connection.execute(
            "SELECT account, SUM(size_bytes) AS total FROM downloads"
            " WHERE account IS NOT NULL GROUP BY account"
        ).fetchall()
    return {row["account"]: row["total"] for row in rows}


# --- Storage Quota counters ------------------------------------------------


def all_account_ids(path: str) -> list[int]:
    """Every Account id, so reconciliation can reset stale counters."""
    with closing(connect(path)) as connection:
        return [
            row[0]
            for row in connection.execute("SELECT id FROM users").fetchall()
        ]


def set_quota_bytes(path: str, account_id: int, quota_bytes: int | None) -> bool:
    """Set an Account's Storage Quota override.

    ``None`` resets to the global default; ``QUOTA_UNLIMITED`` removes the
    cap; any other value is the cap in bytes. Returns True when the
    Account exists.
    """
    with closing(connect(path)) as connection:
        cursor = connection.execute(
            "UPDATE users SET quota_bytes = ? WHERE id = ?",
            (quota_bytes, account_id),
        )
        connection.commit()
    return cursor.rowcount > 0


def adjust_quota_used(path: str, account_id: int, delta: int) -> None:
    """Move an Account's usage counter by ``delta`` bytes.

    Clamped at zero so a lost adjustment can never display as negative
    usage; reconciliation recomputes the truth from the records anyway.
    """
    with closing(connect(path)) as connection:
        connection.execute(
            "UPDATE users SET quota_used_bytes = MAX(0, quota_used_bytes + ?)"
            " WHERE id = ?",
            (delta, account_id),
        )
        connection.commit()


def set_quota_used(path: str, account_id: int, used_bytes: int) -> bool:
    """Overwrite an Account's usage counter (reconciliation's correction)."""
    with closing(connect(path)) as connection:
        cursor = connection.execute(
            "UPDATE users SET quota_used_bytes = ? WHERE id = ?",
            (used_bytes, account_id),
        )
        connection.commit()
    return cursor.rowcount > 0
