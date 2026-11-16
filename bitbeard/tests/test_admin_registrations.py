"""Tests for the admin review half of Registration Requests (ticket #30).

An Admin lists pending requests, approves one into a working Account
(login with the requested credentials succeeds end-to-end), or denies
one. Both decisions land in the Audit Ledger; every endpoint refuses
non-Admins at the boundary; a username taken since submission is
refused at approval, naming the situation so the stale request can be
denied instead.
"""

import json
from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, create_app
from shared import db
from shared.security import verify_password


def make_config(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "bitbeard.db"),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        session_ttl_hours=24.0,
    )
    values.update(overrides)
    return Config(**values)


def _login(client, username="admin", password="hunter2"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


def _submit(client, username="newcomer", password="plunder99", note=None):
    return client.post(
        "/register",
        json={"username": username, "password": password, "note": note},
    )


def _pending(config):
    return db.list_registration_requests(config.db_path)


def _audit(config, action):
    with closing(db.connect(config.db_path)) as connection:
        return connection.execute(
            "SELECT actor, action, details FROM audit WHERE action = ?"
            " ORDER BY id",
            (action,),
        ).fetchall()


# --- The store helpers ------------------------------------------------------


def test_create_account_from_hash_carries_the_stored_hash(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)

    submitted = db.create_registration_request(
        path, username="newcomer", password="plunder99",
        note=None, source_ip="203.0.113.7",
    )
    row = db.get_registration_request(path, submitted)
    account_id = db.create_account_from_hash(
        path, username=row["username"],
        password_hash=row["password_hash"], role="user",
    )

    account = db.find_account(path, "newcomer")
    assert account is not None
    assert account_id == account["id"]
    # The credentials the visitor chose work verbatim — no re-hash.
    assert verify_password("plunder99", account["password_hash"])


def test_create_account_from_hash_refuses_duplicate_usernames(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)
    first = db.create_account(
        path, username="newcomer", password="ropes123", role="user"
    )

    again = db.create_account_from_hash(
        path, username="newcomer", password_hash="stub-hash", role="user"
    )

    assert first is not None
    assert again is None


def test_delete_registration_request_returns_the_row_it_removes(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)
    request_id = db.create_registration_request(
        path, username="newcomer", password="plunder99",
        note="a note", source_ip="203.0.113.7",
    )

    row = db.delete_registration_request(path, request_id)

    assert row["username"] == "newcomer"
    assert row["note"] == "a note"
    assert db.get_registration_request(path, request_id) is None


def test_delete_registration_request_on_an_unknown_id_returns_none(tmp_path):
    path = str(tmp_path / "bitbeard.db")
    db.initialize(path)

    assert db.delete_registration_request(path, 4242) is None


# --- The boundary: Admin Role required ---------------------------------------


def test_the_registration_endpoints_refuse_the_anonymous(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        assert client.get("/admin/registrations").status_code == 401
        assert client.post(
            "/admin/registrations/1/approve"
        ).status_code == 401
        assert client.post(
            "/admin/registrations/1/deny"
        ).status_code == 401


def test_the_registration_endpoints_refuse_the_user_role(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        client.post(
            "/admin/users",
            json={
                "username": "deckhand", "password": "ropes123",
                "role": "user", "quota_bytes": None,
            },
        )
        _login(client, "deckhand", "ropes123")

        assert client.get("/admin/registrations").status_code == 403
        assert client.post(
            "/admin/registrations/1/approve"
        ).status_code == 403
        assert client.post(
            "/admin/registrations/1/deny"
        ).status_code == 403


# --- GET /admin/registrations -------------------------------------------------


def test_the_listing_shows_every_stored_field(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client, note="friend of the crew")

        response = client.get("/admin/registrations")

    assert response.status_code == 200
    requests = response.json()["requests"]
    assert len(requests) == 1
    listed = requests[0]
    assert listed["username"] == "newcomer"
    assert listed["note"] == "friend of the crew"
    assert listed["created_at"]
    assert listed["source_ip"]
    # The stored hash never leaves the store.
    assert "password_hash" not in listed
    assert "password" not in listed


def test_the_listing_is_empty_when_nothing_is_pending(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)

        response = client.get("/admin/registrations")

    assert response.status_code == 200
    assert response.json()["requests"] == []


# --- POST /admin/registrations/{id}/approve -----------------------------------


def test_approval_creates_a_working_login_end_to_end(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]

        response = client.post(f"/admin/registrations/{request_id}/approve")
        assert response.status_code == 200

        # The requested credentials log in, right now, no reset ceremony.
        fresh = TestClient(create_app(config))
        login = fresh.post(
            "/auth/login",
            json={"username": "newcomer", "password": "plunder99"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["role"] == "user"


def test_approval_removes_the_request_and_is_audited(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]

        client.post(f"/admin/registrations/{request_id}/approve")

    assert len(_pending(config)) == 0
    entries = _audit(config, "registration_approved")
    assert len(entries) == 1
    assert entries[0]["actor"] == "admin"
    details = json.loads(entries[0]["details"])
    # The ledger entry references the created Account.
    assert details["registration_request_id"] == request_id
    assert details["username"] == "newcomer"
    account = db.find_account(config.db_path, "newcomer")
    assert details["account_id"] == account["id"]


def test_approval_of_a_username_taken_since_is_refused_plainly(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]

        # An Account with that username appeared after the submission.
        db.create_account(
            config.db_path, username="newcomer",
            password="ropes123", role="user",
        )
        response = client.post(
            f"/admin/registrations/{request_id}/approve"
        )

    assert response.status_code == 409
    # USER-FACING: the situation is named so the Admin can deny instead.
    detail = response.json()["detail"]
    assert "already exists" in detail
    assert "Deny this stale request" in detail
    # Nothing was written: no second Account, request still pending.
    assert len(_pending(config)) == 1
    assert _audit(config, "registration_approved") == []


def test_approval_of_an_unknown_request_is_refused(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        response = client.post("/admin/registrations/4242/approve")

    assert response.status_code == 404


# --- POST /admin/registrations/{id}/deny --------------------------------------


def test_denial_removes_the_request_and_is_audited(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]

        response = client.post(f"/admin/registrations/{request_id}/deny")

    assert response.status_code == 200
    assert len(_pending(config)) == 0
    entries = _audit(config, "registration_denied")
    assert len(entries) == 1
    assert entries[0]["actor"] == "admin"
    details = json.loads(entries[0]["details"])
    # The ledger entry references the refused request.
    assert details["registration_request_id"] == request_id
    assert details["username"] == "newcomer"
    assert details["source_ip"]


def test_denial_still_works_after_a_conflicted_approval(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        db.create_account(
            config.db_path, username="newcomer",
            password="ropes123", role="user",
        )
        assert client.post(
            f"/admin/registrations/{request_id}/approve"
        ).status_code == 409

        response = client.post(f"/admin/registrations/{request_id}/deny")

    assert response.status_code == 200
    assert len(_pending(config)) == 0


def test_denial_of_an_unknown_request_is_refused(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        response = client.post("/admin/registrations/4242/deny")

    assert response.status_code == 404


def test_a_denied_request_cannot_be_denied_twice(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        assert client.post(
            f"/admin/registrations/{request_id}/deny"
        ).status_code == 200

        response = client.post(f"/admin/registrations/{request_id}/deny")

    assert response.status_code == 404
    assert len(_audit(config, "registration_denied")) == 1


def test_denial_does_not_create_an_account(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        client.post(f"/admin/registrations/{request_id}/deny")

    assert db.find_account(config.db_path, "newcomer") is None
