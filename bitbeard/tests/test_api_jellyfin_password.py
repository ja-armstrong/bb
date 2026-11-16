"""Tests for the self-service Jellyfin password flow (ticket #32).

A logged-in user (any Role) sets the password on the Jellyfin user
matching their own Bitbeard username, through the admin API key — which
needs no current password, so the random one generated at provisioning
is simply overwritten and involved nowhere. The endpoint refuses
plainly when the integration is off, the Media Server is not Jellyfin,
or no matching Jellyfin user exists; unauthenticated callers get the
boundary's bare 401 and learn nothing.
"""

import json
from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.jellyfin import (
    JellyfinClient,
    JellyfinProvisioningError,
    JellyfinUserMissingError,
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


def _make_user(config, username="newcomer", password="plunder99"):
    # The store is seeded by the app's lifespan; tests that reach the
    # store before the first request seed it here, the same way.
    db.initialize(
        config.db_path, admin_user=config.admin_user,
        admin_password=config.admin_password,
    )
    account_id = db.create_account(
        config.db_path, username=username, password=password, role="user"
    )
    assert account_id is not None
    return account_id


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
    """The stubbed client the endpoint tests inject.

    Records the (username, password) pairs it was asked to set;
    ``failures`` maps usernames to the error the next call raises.
    """

    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def set_password(self, username, password):
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


def test_set_password_walks_the_documented_endpoints():
    script = [
        (200, [{"Name": "someone-else", "Id": "u0"}, {"Name": "newcomer", "Id": "u1"}]),
        (204, None),
    ]
    transport = _stub_transport(script)
    client = JellyfinClient("http://jellyfin.test:8096/", "jf-key", transport)

    client.set_password("newcomer", "s3cret-pw")

    methods_urls = [(r["method"], r["url"]) for r in transport.requests]
    assert methods_urls == [
        ("GET", "http://jellyfin.test:8096/Users"),
        ("POST", "http://jellyfin.test:8096/Users/u1/Password"),
    ]
    # The token rides in the header on every call, never in a URL.
    assert all(
        r["headers"] == {"X-Emby-Token": "jf-key"} for r in transport.requests
    )
    # The admin key needs no current password: the set carries only the
    # matched user's id and the new one.
    password_call = transport.requests[1]
    assert password_call["json"] == {"Id": "u1", "NewPw": "s3cret-pw"}
    assert "CurrentPw" not in password_call["json"]


def test_set_password_matches_the_username_case_insensitively():
    transport = _stub_transport([(200, [{"Name": "Newcomer", "Id": "u9"}]), (204, None)])
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    client.set_password("newcomer", "pw")

    assert transport.requests[1]["url"].endswith("/Users/u9/Password")


def test_set_password_refuses_when_no_user_matches():
    transport = _stub_transport([(200, [{"Name": "someone-else", "Id": "u0"}])])
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    try:
        client.set_password("newcomer", "pw")
        raise AssertionError("set_password should have raised")
    except JellyfinUserMissingError as exc:
        assert "no Jellyfin user" in str(exc)
        assert "newcomer" in str(exc)
    # The absence was seen in the listing; no password call happened.
    assert len(transport.requests) == 1


def test_set_password_names_a_rejected_token():
    transport = _stub_transport([(401, None)])
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    try:
        client.set_password("newcomer", "pw")
        raise AssertionError("set_password should have raised")
    except JellyfinProvisioningError as exc:
        assert "401" in str(exc) and "token" in str(exc)


def test_set_password_names_a_refused_set():
    transport = _stub_transport(
        [(200, [{"Name": "newcomer", "Id": "u1"}]), (403, None)]
    )
    client = JellyfinClient("http://jellyfin.test:8096", "jf-key", transport)

    try:
        client.set_password("newcomer", "pw")
        raise AssertionError("set_password should have raised")
    except JellyfinProvisioningError as exc:
        assert "refused to set" in str(exc)


# --- The endpoint, with the integration on ------------------------------------


def _enabled(tmp_path, monkeypatch, **config_overrides):
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path, jellyfin_api_token="jf-key", **config_overrides)
    stub = StubJellyfin()
    app = create_app(config, jellyfin_client=stub)
    return config, stub, app


def test_a_user_role_account_sets_their_own_jellyfin_password(
    tmp_path, monkeypatch
):
    config, stub, app = _enabled(tmp_path, monkeypatch)
    _make_user(config)

    with TestClient(app) as client:
        _login(client, "newcomer", "plunder99")
        response = client.post(
            "/me/jellyfin-password", json={"new_password": "my-own-pw-1"}
        )

    assert response.status_code == 200, response.text
    # The call carried the caller's own username, not another's.
    assert stub.calls == [("newcomer", "my-own-pw-1")]
    # USER-FACING
    assert response.json()["message"] == (
        "Password set — log into Jellyfin with it."
    )


def test_success_is_audited_without_the_password(tmp_path, monkeypatch):
    config, stub, app = _enabled(tmp_path, monkeypatch)
    _make_user(config)

    with TestClient(app) as client:
        _login(client, "newcomer", "plunder99")
        client.post("/me/jellyfin-password", json={"new_password": "my-own-pw-1"})

    entries = _audit(config, "jellyfin_password_set")
    assert len(entries) == 1
    assert entries[0]["actor"] == "newcomer"
    assert entries[0]["details"] == {}
    assert "my-own-pw-1" not in json.dumps(entries)


def test_unauthenticated_callers_are_refused_and_learn_nothing(
    tmp_path, monkeypatch
):
    config, stub, app = _enabled(tmp_path, monkeypatch)
    _make_user(config)

    with TestClient(app) as client:
        response = client.post(
            "/me/jellyfin-password", json={"new_password": "my-own-pw-1"}
        )

    assert response.status_code == 401
    assert stub.calls == []


def test_short_passwords_are_refused_at_the_boundary(tmp_path, monkeypatch):
    config, stub, app = _enabled(tmp_path, monkeypatch)
    _make_user(config)

    with TestClient(app) as client:
        _login(client, "newcomer", "plunder99")
        response = client.post("/me/jellyfin-password", json={"new_password": "short"})

    assert response.status_code == 422
    assert stub.calls == []


# --- The three refusals, each with its plain message --------------------------


def test_refuses_when_the_integration_is_disabled(tmp_path, monkeypatch):
    # The Media Server is Jellyfin, but no API token: integration off.
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path)
    _make_user(config)
    stub = StubJellyfin()

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client, "newcomer", "plunder99")
        response = client.post(
            "/me/jellyfin-password", json={"new_password": "my-own-pw-1"}
        )

    assert response.status_code == 403
    assert "not set up" in response.json()["detail"]
    assert stub.calls == []


def test_refuses_when_the_media_server_is_not_jellyfin(tmp_path, monkeypatch):
    # The token is set, but the stack boots Plex: no Jellyfin to touch.
    monkeypatch.setenv("COMPOSE_PROFILES", "plex")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")
    _make_user(config)
    stub = StubJellyfin()

    with TestClient(create_app(config, jellyfin_client=stub)) as client:
        _login(client, "newcomer", "plunder99")
        response = client.post(
            "/me/jellyfin-password", json={"new_password": "my-own-pw-1"}
        )

    assert response.status_code == 403
    assert "not Jellyfin" in response.json()["detail"]
    assert stub.calls == []


def test_refuses_when_no_jellyfin_user_matches(tmp_path, monkeypatch):
    config, stub, app = _enabled(
        tmp_path,
        monkeypatch,
    )
    stub.failures["newcomer"] = JellyfinUserMissingError(
        "no Jellyfin user named 'newcomer' exists"
    )
    _make_user(config)

    with TestClient(app) as client:
        _login(client, "newcomer", "plunder99")
        response = client.post(
            "/me/jellyfin-password", json={"new_password": "my-own-pw-1"}
        )

    assert response.status_code == 404
    # A logged-in caller is told the absence plainly.
    assert "No Jellyfin user named 'newcomer'" in response.json()["detail"]


def test_an_unreachable_jellyfin_is_a_502(tmp_path, monkeypatch):
    config, stub, app = _enabled(tmp_path, monkeypatch)
    stub.failures["newcomer"] = JellyfinProvisioningError(
        "Jellyfin could not be reached"
    )
    _make_user(config)

    with TestClient(app) as client:
        _login(client, "newcomer", "plunder99")
        response = client.post(
            "/me/jellyfin-password", json={"new_password": "my-own-pw-1"}
        )

    assert response.status_code == 502
    assert "could not be reached" in response.json()["detail"]
    # The failed attempt is not audited as a success.
    assert _audit(config, "jellyfin_password_set") == []


# --- The capability flag on /auth/me ------------------------------------------


def test_auth_me_carries_the_capability_flag_when_enabled(tmp_path, monkeypatch):
    config, stub, app = _enabled(tmp_path, monkeypatch)

    with TestClient(app) as client:
        _login(client)
        flag = client.get("/auth/me").json()["jellyfin_password_available"]

    assert flag is True


def test_auth_me_flag_is_false_when_the_integration_is_disabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        flag = client.get("/auth/me").json()["jellyfin_password_available"]

    assert flag is False


def test_auth_me_flag_is_false_when_the_media_server_is_plex(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMPOSE_PROFILES", "plex")
    config = make_config(tmp_path, jellyfin_api_token="jf-key")

    with TestClient(create_app(config)) as client:
        _login(client)
        flag = client.get("/auth/me").json()["jellyfin_password_available"]

    assert flag is False
