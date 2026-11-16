"""Tests for public Registration Requests (ticket #29).

The submission half: an anonymous visitor files a request through the
API's /register endpoint, it lands in the store with a hashed password,
and the Audit Ledger records the submission. The admin review half is
ticket #30 and does not exist yet.
"""

import json
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import api.main
from api.config import Config
from api.main import create_app
from shared import db
from shared.security import verify_password


def make_config(db_path, **overrides):
    values = dict(
        db_path=str(db_path),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        session_ttl_hours=24.0,
    )
    values.update(overrides)
    return Config(**values)


def _insert_account(config, username, password, role="user"):
    """Create an Account directly in the store, for conflict tests."""
    db.initialize(config.db_path)
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, "stub-hash", role, db.utc_now()),
        )
        connection.commit()


def _registration_rows(config):
    with closing(db.connect(config.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM registration_requests"
        ).fetchall()


def _audit_rows(config, action="registration_requested"):
    with closing(db.connect(config.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM audit WHERE action = ?", (action,)
        ).fetchall()


def _post(client, *, headers=None, **fields):
    payload = {"username": "newcomer", "password": "plunder99"}
    payload.update(fields)
    return client.post("/register", json=payload, headers=headers)


# --- The store helpers ----------------------------------------------------


def test_create_registration_request_hashes_the_password(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)

    request_id = db.create_registration_request(
        path,
        username="newcomer",
        password="plunder99",
        note=None,
        source_ip="203.0.113.7",
    )

    row = db.pending_registration_request(path, "newcomer")
    assert request_id is not None
    assert row["password_hash"] != "plunder99"
    assert verify_password("plunder99", row["password_hash"])
    assert row["note"] is None
    assert row["source_ip"] == "203.0.113.7"
    assert row["created_at"]


def test_duplicate_registration_request_username_returns_none(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)

    first = db.create_registration_request(
        path, username="newcomer", password="plunder99",
        note=None, source_ip="203.0.113.7",
    )
    second = db.create_registration_request(
        path, username="newcomer", password="other-pass",
        note=None, source_ip="203.0.113.8",
    )

    assert first is not None
    assert second is None
    assert len(_registration_rows(make_config(path))) == 1


# --- POST /register (happy path) -------------------------------------------


def test_submission_stores_a_pending_request_with_a_hashed_password(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = _post(
            client,
            username="newcomer",
            password="plunder99",
            note="friend of the crew",
        )

    assert response.status_code == 200
    rows = _registration_rows(config)
    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "newcomer"
    assert row["note"] == "friend of the crew"
    # Hashed with the same mechanism Accounts use — never the plaintext.
    assert row["password_hash"] != "plunder99"
    assert "plunder99" not in row["password_hash"]
    assert verify_password("plunder99", row["password_hash"])


def test_submission_is_audited_with_the_anonymous_actor(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = _post(client, username="newcomer")
        assert response.status_code == 200

    rows = _audit_rows(config)
    assert len(rows) == 1
    entry = rows[0]
    assert entry["actor"] == "anonymous"
    details = json.loads(entry["details"])
    assert details["username"] == "newcomer"
    # The ledger's details carry the source IP; the Actor stays anonymous.
    assert details["source_ip"]


def test_nothing_user_facing_echoes_or_stores_the_plaintext(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = _post(client)

    body = response.text
    assert "plunder99" not in body
    with open(config.db_path, "rb") as store:
        assert b"plunder99" not in store.read()
    for entry in _audit_rows(config):
        assert "plunder99" not in (entry["details"] or "")


def test_a_blank_note_is_stored_as_null_not_empty_string(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        _post(client, note="   ")

    row = _registration_rows(config)[0]
    assert row["note"] is None


def test_a_too_short_password_is_refused_by_the_shared_validation(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = _post(client, password="short")

    assert response.status_code == 422
    assert len(_registration_rows(config)) == 0


def test_a_whitespace_only_username_is_refused(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = _post(client, username="   ")

    assert response.status_code == 400
    assert len(_registration_rows(config)) == 0


# --- Conflicts ---------------------------------------------------------------


def test_an_existing_account_conflict_is_named_plainly(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    _insert_account(config, "newcomer", "ropes123")

    with TestClient(create_app(config)) as client:
        response = _post(client)

    assert response.status_code == 409
    # USER-FACING: the message names the situation without leaking hashes.
    assert "Account with that username already exists" in response.json()["detail"]
    assert len(_registration_rows(config)) == 0


def test_a_pending_request_conflict_is_named_plainly(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert _post(client).status_code == 200
        response = _post(client)

    assert response.status_code == 409
    assert "already pending" in response.json()["detail"]
    assert len(_registration_rows(config)) == 1


# --- Per-IP throttle ---------------------------------------------------------


@pytest.fixture()
def throttled(monkeypatch):
    """A tight limit so a test can trip it in a few requests."""
    monkeypatch.setattr(api.main, "REGISTRATION_LIMIT_PER_HOUR", 2)


def _from(ip):
    return {"X-Forwarded-For": ip}


def test_the_per_ip_throttle_trips_with_a_clear_message(tmp_path, throttled):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert _post(client, username="crew0", headers=_from("203.0.113.7")).status_code == 200
        assert _post(client, username="crew1", headers=_from("203.0.113.7")).status_code == 200
        response = _post(client, username="crew2", headers=_from("203.0.113.7"))

    assert response.status_code == 429
    # USER-FACING: non-hostile, says what to do.
    assert "Too many registration requests" in response.json()["detail"]
    # The throttled attempt filed nothing.
    assert len(_registration_rows(config)) == 2


def test_the_throttle_is_per_address_not_global(tmp_path, throttled):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert _post(client, username="crew0", headers=_from("203.0.113.7")).status_code == 200
        assert _post(client, username="crew1", headers=_from("203.0.113.7")).status_code == 200
        # A different address has its own bucket: despite .7's budget
        # being spent, a fresh address still files a request.
        assert _post(client, username="crew2", headers=_from("203.0.113.9")).status_code == 200


def test_a_throttled_attempt_never_reaches_the_store_or_ledger(
    tmp_path, throttled
):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert _post(client, username="crew0", headers=_from("203.0.113.7")).status_code == 200
        assert _post(client, username="crew1", headers=_from("203.0.113.7")).status_code == 200
        assert _post(client, username="crew2", headers=_from("203.0.113.7")).status_code == 429

    assert len(_audit_rows(config)) == 2


def test_the_throttle_counts_a_conflict_attempt_too(tmp_path, throttled):
    """Attempts count whether or not they're accepted: no free probing."""
    config = make_config(tmp_path / "bitbeard.db")
    _insert_account(config, "taken", "ropes123")

    with TestClient(create_app(config)) as client:
        # Two refused attempts burn the address's whole budget...
        assert _post(client, username="taken", headers=_from("1.1.1.1")).status_code == 409
        assert _post(client, username="taken", headers=_from("1.1.1.1")).status_code == 409
        # ...so a genuinely new username from the same address is throttled.
        assert _post(client, username="newcomer", headers=_from("1.1.1.1")).status_code == 429


def test_a_single_entry_xff_throttles_and_ledgers_as_that_address(
    tmp_path, throttled
):
    """The contract's normal case (docs/proxy.md): the front proxy's
    overwrite leaves a single-entry XFF, and the API reads that one
    entry exactly as it always has — one bucket per address, and the
    ledger's source IP is the header's value."""
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert _post(client, username="crew0", headers=_from("203.0.113.7")).status_code == 200
        assert _post(client, username="crew1", headers=_from("203.0.113.7")).status_code == 200
        response = _post(client, username="crew2", headers=_from("203.0.113.7"))

    assert response.status_code == 429
    # Two accepted submissions, both ledgered under the header's address.
    rows = _audit_rows(config)
    assert len(rows) == 2
    for entry in rows:
        assert json.loads(entry["details"])["source_ip"] == "203.0.113.7"


# --- The boundary: unauthenticated by design --------------------------------


def test_register_is_open_but_surrounding_routes_are_not(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        # The endpoint itself is the public door.
        assert _post(client).status_code == 200
        # Nothing else opened up with it.
        assert client.get("/auth/me").status_code == 401
        assert client.get("/admin/ping").status_code == 401
