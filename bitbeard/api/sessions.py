"""Server-side Sessions: issue, resolve and revoke.

A Session is login state stored in the shared SQLite store and presented to
the Frontend as an HTTP-only cookie (ADR-0002). The cookie carries nothing but
a random 256-bit token; all authority lives in the ``sessions`` row, so any
Session can be revoked server-side by deleting it. The store never needs to
trust the cookie beyond the lookup.

The clock and time-to-live are injectable so tests can move time forward
without sleeping.
"""

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shared import db

#: Length of the session token in bytes: 256 bits of entropy, per the spec.
TOKEN_BYTES = 32


@dataclass(frozen=True)
class Session:
    """One issued Session: its token, owner and lifetime."""

    token: str
    account_id: int
    created_at: str
    expires_at: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format(moment: datetime) -> str:
    """A timestamp in the one format the store writes (see ``shared.db``)."""
    return moment.isoformat(timespec="seconds")


class SessionStore:
    """Creates, resolves and revokes Sessions in the shared store.

    ``now`` and ``ttl_hours`` are injectable for tests; production uses wall
    time and the configured ``SESSION_TTL_HOURS``.
    """

    def __init__(
        self,
        db_path: str,
        *,
        ttl_hours: float = 24.0,
        now=_utcnow,
    ):
        self.db_path = db_path
        self.ttl = timedelta(hours=ttl_hours)
        self._now = now

    def issue(self, account_id: int) -> Session:
        """Create a fresh Session for ``account_id``."""
        created_at = self._now()
        expires_at = created_at + self.ttl
        token = secrets.token_hex(TOKEN_BYTES)
        with closing(db.connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO sessions (token, account_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, account_id, _format(created_at), _format(expires_at)),
            )
            connection.commit()
        return Session(
            token=token,
            account_id=account_id,
            created_at=_format(created_at),
            expires_at=_format(expires_at),
        )

    def resolve(self, token: str) -> Session | None:
        """Return the live Session for ``token``, or None when unknown/expired.

        An expired Session is deleted on sight so dead rows do not accumulate
        in the store.
        """
        with closing(db.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                return None
            if self._now() >= datetime.fromisoformat(row["expires_at"]):
                connection.execute(
                    "DELETE FROM sessions WHERE token = ?", (token,)
                )
                connection.commit()
                return None
        return Session(
            token=row["token"],
            account_id=row["account_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def revoke(self, token: str) -> bool:
        """Delete a Session. Returns True when it existed."""
        with closing(db.connect(self.db_path)) as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE token = ?", (token,)
            )
            connection.commit()
        return cursor.rowcount > 0

    def revoke_all_for_account(self, account_id: int, *, keep: str | None = None) -> int:
        """Revoke every Session of ``account_id`` except ``keep``.

        Used when an Account's password changes: sessions the holder did not
        create (e.g. another browser) must not survive the credential change.
        Returns the number of revoked Sessions.
        """
        with closing(db.connect(self.db_path)) as connection:
            if keep is None:
                cursor = connection.execute(
                    "DELETE FROM sessions WHERE account_id = ?", (account_id,)
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM sessions WHERE account_id = ? AND token != ?",
                    (account_id, keep),
                )
            connection.commit()
        return cursor.rowcount
