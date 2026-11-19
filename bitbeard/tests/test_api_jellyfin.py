"""Tests for the optional Jellyfin provisioning on registration approval
(ticket #31).

The integration is off unless ``JELLYFIN_URL`` + ``JELLYFIN_API_TOKEN``
are set AND the Media Server is Jellyfin. When on, approving a
Registration Request provisions the matching Jellyfin user with a random
password that is never displayed or logged; every failure is recorded —
in the Audit Ledger entry and the admin flash — without failing the
approval. When off, approval behaves exactly as before and makes zero
calls.
"""

import json
from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api import jellyfin
from api.jellyfin import (
    JellyfinClient,
    JellyfinProvisioningError,
    generate_password,
)
from api.main import COOKIE_NAME, create_app
from shared import db


def make_config(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "bitbeard.db"),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        session_ttl_hours=24.0,
        jellyfin_url="http://jellyfin.test:8096",
        jellyfin_api_token="",
    )
    values.update(overrides)
    return Config(**values)


def _login(client, username="admin", password="hunter2"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


def _submit(client, username="newcomer", password="plunder99"):
    return client.post(
        "/register", json={"username": username, "password": password}
    )


def _pending(config):
    return db.list_registration_requests(config.db_path)


def _approve(client, request_id):
    return client.post(f"/admin/registrations/{request_id}/approve")


def _audit(config, action):
    with closing(db.connect(config.db_path)) as connection:
        rows = connection.execute(
            "SELECT actor, action, details FROM audit WHERE action = ?"
            " ORDER BY id",
            (action,),
        ).fetchall()
    return [
        {**dict(row), "details": json.loads(row["details"])} for row in rows
    ]


class StubJellyfin:
    """The stubbed Jellyfin client the tests inject.

    Records the (username, password) pairs it was asked to create;
    ``failures`` maps usernames to the error the next call raises.
    """

    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def create_user(self, username, password):
        self.calls.append((username, password))
        failure = self.failures.get(username)
        if failure is not None:
            raise failure


# --- The client, against a stubbed transport ---------------------------------


def _stub_transport(script):
    """A transport that plays back canned (status, payload) answers per
    call, in order, and records everything it saw."""

    class Recording:
        requests = []

        def __call__(self, method, url, *, headers, json):
            Recording.requests.append(
                {"method": method, "url": url, "headers": headers, "json": json}
            )
            return script.pop(0)

    return Recording()


def test_generate_password_is_random_and_reasonable():
    passwords = {generate_password() for _ in range(20)}
    assert len(passwords) == 20
    for password in passwords:
        assert len(password) >= 16
        # URL-safe alphabet only: it never needs escaping anywhere.
        assert all(c.isalnum() or c in "-_" for c in password)


def test_create_user_walks_the_documented_endpoints():
    script = [
        (200, [{"Name": "someone-else", "Id": "u0"}]),
        (200, {"Name": "newcomer", "Id": "u1"}),
        (204, None),
    ]
    transport = _stub_transport(script)
    client = JellyfinClient("http://jellyfin.test:8096/", "jf-key", transport)

    client.create_user("newcomer", "s3cret-pw")

    methods_urls = [(r["method"], r["url"]) for r in transport.requests]
    assert methods_urls == [
        ("GET", "http://jellyfin.test:8096/Users"),
        ("POST", "http://jellyfin.test:8096/Users/New"),
        ("POST", "http://jellyfin.test:8096/Users/u1/Password"),
    ]
    # The token rides in the Authorization header on every call, never in
    # a URL (recent Jellyfin rejects a bare API key in X-Emby-Token).
    assert all(
        r["headers"] == {"Authorization": 'MediaBrowser Token="jf-key"'}
        for r in transport.requests
    )
    create, password = transport.requests[1], transport.requests[2]
    assert create["json"] == {
        "Name": "newcomer",
        "Policy": {
            "AuthenticationProviderId": jellyfin.DEFAULT_AUTH_PROVIDER,
            "PasswordResetProviderId": jellyfin.DEFAULT_RESET_PROVIDER,
        },
    }
    assert password["json"] == {"Id": "u1", "CurrentPw": "", "NewPw": "s3cret-pw"}


def test_create_user_refuses_a_taken_username():
    transport = _stub_transport([(200, [{"Name": "Newcomer", "Id": "u1"}])])
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    try:
        client.create_user("newcomer", "pw")
        raise AssertionError("create_user should have raised")
    except JellyfinProvisioningError as exc:
        # The taken name (case-insensitive) is named; no create call ran.
        assert "already exists" in str(exc)
        assert "newcomer" in str(exc)
        assert len(transport.requests) == 1


def test_create_user_names_a_rejected_token():
    transport = _stub_transport([(401, None)])
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    try:
        client.create_user("newcomer", "pw")
        raise AssertionError("create_user should have raised")
    except JellyfinProvisioningError as exc:
        assert "401" in str(exc) and "token" in str(exc)


def test_create_user_names_an_unreachable_server():
    transport = _stub_transport([(200, [{"Name": "x", "Id": "u0"}]), (502, None)])
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    try:
        client.create_user("newcomer", "pw")
        raise AssertionError("create_user should have raised")
    except JellyfinProvisioningError as exc:
        assert "create" in str(exc)
    # The password was never sent on the failed create.
    assert transport.requests[1]["json"]["Name"] == "newcomer"
    assert "NewPw" not in transport.requests[1]["json"]


# --- Approve with the integration off ----------------------------------------


def test_disabled_approval_makes_no_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    config = make_config(tmp_path)
    stub = StubJellyfin()

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        response = _approve(client, request_id)

    assert response.status_code == 200
    # Zero new behaviour, zero calls — the stub would have recorded them.
    assert stub.calls == []
    assert response.json()["jellyfin_provisioning"] == {"status": "off"}
    details = _audit(config, "registration_approved")[0]["details"]
    assert details["jellyfin_provisioning"] == {"status": "off"}
    assert db.find_account(config.db_path, "newcomer") is not None


def test_enabled_but_plex_media_server_makes_no_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROFILES", "plex")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    stub = StubJellyfin()

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        response = _approve(client, request_id)

    assert response.status_code == 200
    assert stub.calls == []
    assert response.json()["jellyfin_provisioning"] == {"status": "off"}


# --- Approve with the integration on -----------------------------------------


def test_enabled_approval_creates_the_jellyfin_user(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    stub = StubJellyfin()

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        response = _approve(client, request_id)

    assert response.status_code == 200
    assert len(stub.calls) == 1
    username, password = stub.calls[0]
    # The same username as the Account; the password is real and random.
    assert username == "newcomer"
    assert len(password) >= 16

    # Success is named in the response and the ledger entry.
    assert response.json()["jellyfin_provisioning"] == {"status": "succeeded"}
    assert "Jellyfin user was created" in response.json()["message"]
    details = _audit(config, "registration_approved")[0]["details"]
    assert details["jellyfin_provisioning"] == {"status": "succeeded"}
    assert "password" not in json.dumps(details)


def test_the_generated_password_never_leaks_into_logs_or_ledger(
    tmp_path, monkeypatch, caplog
):
    import logging

    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    stub = StubJellyfin()

    with caplog.at_level(logging.DEBUG):
        with TestClient(create_app(config, jellyfin_client=stub)) as client:
            _login(client)
            _submit(client)
            request_id = _pending(config)[0]["id"]
            _approve(client, request_id)

    password = stub.calls[0][1]
    assert password not in caplog.text
    ledger = _audit(config, "registration_approved")[0]
    assert password not in json.dumps(ledger)


def test_provisioning_failure_does_not_fail_the_approval(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    stub = StubJellyfin(
        failures={
            "newcomer": JellyfinProvisioningError(
                "a Jellyfin user named 'newcomer' already exists"
            )
        }
    )

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        response = _approve(client, request_id)

    # The Account was still created and the request consumed.
    assert response.status_code == 200
    assert db.find_account(config.db_path, "newcomer") is not None
    assert len(_pending(config)) == 0

    # The flash names what did not happen and why.
    body = response.json()
    assert body["jellyfin_provisioning"]["status"] == "failed"
    assert "Jellyfin user was NOT created" in body["message"]
    assert "already exists" in body["message"]

    # The ledger records the failure with its reason.
    details = _audit(config, "registration_approved")[0]["details"]
    assert details["jellyfin_provisioning"] == {
        "status": "failed",
        "reason": "a Jellyfin user named 'newcomer' already exists",
    }


def test_an_unreachable_server_is_recorded_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    stub = StubJellyfin(
        failures={
            "newcomer": JellyfinProvisioningError(
                "Jellyfin could not be reached"
            )
        }
    )

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        response = _approve(client, request_id)

    assert response.status_code == 200
    assert "could not be reached" in response.json()["message"]
    details = _audit(config, "registration_approved")[0]["details"]
    assert details["jellyfin_provisioning"]["status"] == "failed"


def test_the_real_client_is_built_from_the_config(tmp_path, monkeypatch):
    """With no injected client, the enabled config builds the real one —
    against the default transport, patched here."""
    import api.jellyfin

    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(
        tmp_path,
        jellyfin_url="http://jellyfin.test:8096",
        jellyfin_api_token="jf-key",
    )
    script = [(200, []), (200, {"Id": "u1"}), (204, None)]
    seen = []

    def fake_transport(method, url, *, headers, json):
        seen.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        return script.pop(0)

    monkeypatch.setattr(api.jellyfin, "_default_transport", fake_transport)

    with TestClient(create_app(config)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        _approve(client, request_id)

    # The real client reached the config's URL with the config's token.
    assert seen[0] == {
        "method": "GET",
        "url": "http://jellyfin.test:8096/Users",
        "headers": {"Authorization": 'MediaBrowser Token="jf-key"'},
        "json": None,
    }
    assert seen[1]["url"] == "http://jellyfin.test:8096/Users/New"
    assert seen[2]["url"] == "http://jellyfin.test:8096/Users/u1/Password"


def test_a_space_separated_profiles_value_counts_as_jellyfin(
    tmp_path, monkeypatch
):
    """The wizard writes COMPOSE_PROFILES space-separated."""
    monkeypatch.setenv("COMPOSE_PROFILES", "bot jellyfin")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    stub = StubJellyfin()

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client)
        _submit(client)
        request_id = _pending(config)[0]["id"]
        _approve(client, request_id)

    assert len(stub.calls) == 1
