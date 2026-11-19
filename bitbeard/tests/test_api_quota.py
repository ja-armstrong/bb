"""Tests for Storage Quota enforcement at the API boundary (ticket #5).

The pre-check lives in the API (it owns identity and the cap config), the
per-Account quota endpoint is admin-gated and audited, and /me/quota feeds
the Webui's usage-vs-cap display.
"""

from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, create_app
from api.manager_proxy import ProxyResponse
from shared import db
from shared.security import hash_password


MANAGER_URL = "http://manager.test"


def make_config(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "bitbeard.db"),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=0.001,  # ~1 MiB, small enough to trip in tests
        manager_url=MANAGER_URL,
    )
    values.update(overrides)
    return Config(**values)


def _insert_account(config, username, password="ropes123", role="user",
                    quota_bytes=None):
    """Create an Account directly; Account creation arrives with #8."""
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, quota_bytes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, hash_password(password), role, quota_bytes, db.utc_now()),
        )
        connection.commit()
        return cursor.lastrowid


def _record_download(config, account_id, size_bytes, status="completed"):
    """Put a Download record in the store as the manager would leave it."""
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO downloads (account, name, category, save_path,"
            " size_bytes, status, submitted_at) VALUES (?, 'Some Movies',"
            " 'Movies', '/media/Movies/1', ?, ?, ?)",
            (account_id, size_bytes, status, db.utc_now()),
        )
        connection.commit()
        return cursor.lastrowid


def _login(client, username, password="ropes123"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


class FakeTransport:
    """Records every proxied call; answers with canned ProxyResponses."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, method, url, *, headers, params=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        if self.responses:
            return self.responses.pop(0)
        return ProxyResponse(
            200,
            {"message": "Download started successfully", "download_id": 1},
        )

    @property
    def last(self):
        return self.calls[-1]


def _submit(client, size_bytes=0, **json_extra):
    body = {
        "magnet_link": "magnet:?xt=urn:btih:abcdef",
        "category": "Movies",
        "size_bytes": size_bytes,
    }
    body.update(json_extra)
    return client.post("/download", json=body)


# --- The submission pre-check ----------------------------------------------


def test_over_cap_submission_is_rejected_with_a_clear_error(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        _record_download(config, account_id, 2 * 1024 * 1024)  # 2 MiB on disk
        _login(client, "deckhand")
        response = _submit(client, size_bytes=0)

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail.startswith("Quota exceeded")
    assert "Delete something first." in detail
    # The Download never reached the manager: no over-commit state exists.
    assert transport.calls == []


def test_the_pre_check_counts_in_flight_sizes_too(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        _record_download(
            config, account_id, 512 * 1024, status="in_flight"
        )
        _record_download(
            config, account_id, 512 * 1024, status="in_flight"
        )
        _login(client, "deckhand")
        response = _submit(client, size_bytes=512 * 1024)

    assert response.status_code == 403
    assert transport.calls == []


def test_under_cap_submissions_are_proxied(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        _record_download(config, account_id, 512 * 1024)
        _login(client, "deckhand")
        response = _submit(client, size_bytes=256 * 1024)

    assert response.status_code == 200
    assert transport.last["url"] == f"{MANAGER_URL}/download"
    # The size and title ride along so the manager's record is accurate.
    assert transport.last["json"]["size_bytes"] == 256 * 1024


def test_a_per_account_override_replaces_the_global_default(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        _insert_account(config, "deckhand", quota_bytes=1024)
        _login(client, "deckhand")
        response = _submit(client, size_bytes=1025)

    assert response.status_code == 403
    assert "1.00 KB" in response.json()["detail"]
    assert transport.calls == []


def test_admin_submissions_bypass_the_quota_check(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        _login(client, "admin", "hunter2")
        response = _submit(client, size_bytes=100 * 1024 * 1024)

    assert response.status_code == 200
    assert transport.last["url"] == f"{MANAGER_URL}/download"


def test_unlimited_accounts_bypass_the_quota_check(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        _insert_account(config, "deckhand", quota_bytes=db.QUOTA_UNLIMITED)
        _login(client, "deckhand")
        response = _submit(client, size_bytes=100 * 1024 * 1024)

    assert response.status_code == 200


def test_a_linked_discord_user_is_quota_checked_against_their_account(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(
            config, "deckhand", quota_bytes=1024, role="user"
        )
        with closing(db.connect(config.db_path)) as connection:
            connection.execute(
                "UPDATE users SET discord_id = 'salty#1234' WHERE id = ?",
                (account_id,),
            )
            connection.commit()
        # The Bot's shape: SERVICE_TOKEN bearer plus the acting Discord user.
        client.headers["Authorization"] = "Bearer service-token"
        client.headers["X-Bitbeard-Discord"] = "salty#1234"
        response = _submit(client, size_bytes=2048)

    assert response.status_code == 403
    assert transport.calls == []


def test_an_unlinked_discord_user_has_no_cap_to_check(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        client.headers["Authorization"] = "Bearer service-token"
        client.headers["X-Bitbeard-Discord"] = "salty#1234"
        client.headers["X-Bitbeard-Discord-Name"] = "salty"
        response = _submit(client, size_bytes=100 * 1024 * 1024)

    assert response.status_code == 200
    # No Account, so no owner id is forwarded: the manager records the
    # discord:<name> Actor instead.
    assert "X-Bitbeard-Account-Id" not in transport.last["headers"]


def test_the_account_id_rides_along_as_a_trusted_header(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        _login(client, "deckhand")
        _submit(client)

    assert transport.last["headers"]["X-Bitbeard-Account-Id"] == str(account_id)


# --- GET /me/quota -----------------------------------------------------------


def test_me_quota_reports_usage_against_the_default_cap(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        account_id = _insert_account(config, "deckhand")
        _record_download(config, account_id, 512 * 1024)
        _login(client, "deckhand")
        response = client.get("/me/quota")

    assert response.status_code == 200
    body = response.json()
    assert body["used_bytes"] == 512 * 1024
    assert body["cap_bytes"] == int(0.001 * 1024 ** 3)
    assert body["exempt"] is False
    assert body["unlimited"] is False


def test_me_quota_reports_overrides_unlimited_and_admins(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _insert_account(config, "deckhand", quota_bytes=1024)
        _insert_account(config, "freebooter", quota_bytes=db.QUOTA_UNLIMITED)
        _login(client, "deckhand")
        assert client.get("/me/quota").json()["cap_bytes"] == 1024
        _login(client, "freebooter")
        assert client.get("/me/quota").json() == {
            "used_bytes": 0,
            "cap_bytes": None,
            "unlimited": True,
            "exempt": False,
            "override_bytes": db.QUOTA_UNLIMITED,
        }
        _login(client, "admin", "hunter2")
        admin_body = client.get("/me/quota").json()
        assert admin_body["exempt"] is True
        assert admin_body["cap_bytes"] is None


def test_me_quota_requires_a_session(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        assert client.get("/me/quota").status_code == 401


# --- POST /admin/users/{id}/quota -------------------------------------------


def test_admin_can_set_a_quota_override_and_it_is_audited(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        account_id = _insert_account(config, "deckhand")
        _login(client, "admin", "hunter2")
        response = client.post(
            f"/admin/users/{account_id}/quota", json={"quota_bytes": 2048}
        )

    assert response.status_code == 200
    assert response.json() == {
        "username": "deckhand",
        "used_bytes": 0,
        "cap_bytes": 2048,
        "unlimited": False,
        "exempt": False,
        "override_bytes": 2048,
    }
    with closing(db.connect(config.db_path)) as connection:
        stored = connection.execute(
            "SELECT quota_bytes FROM users WHERE id = ?", (account_id,)
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT actor, action, details FROM audit WHERE action ="
            " 'quota_assigned'"
        ).fetchall()
    assert stored == 2048
    assert rows[0]["actor"] == "admin"
    assert '"quota_bytes": 2048' in rows[0]["details"]


def test_admin_can_reset_to_the_default_and_set_unlimited(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        account_id = _insert_account(config, "deckhand", quota_bytes=2048)
        _login(client, "admin", "hunter2")

        default = client.post(
            f"/admin/users/{account_id}/quota", json={"quota_bytes": None}
        )
        unlimited = client.post(
            f"/admin/users/{account_id}/quota",
            json={"quota_bytes": db.QUOTA_UNLIMITED},
        )

    assert default.json()["override_bytes"] is None
    assert default.json()["cap_bytes"] == int(0.001 * 1024 ** 3)
    assert unlimited.json()["unlimited"] is True
    assert unlimited.json()["cap_bytes"] is None


def test_the_quota_endpoint_is_admin_gated(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        account_id = _insert_account(config, "deckhand")
        _login(client, "deckhand")
        response = client.post(
            f"/admin/users/{account_id}/quota", json={"quota_bytes": 2048}
        )

    assert response.status_code == 403


def test_the_quota_endpoint_refuses_unknown_accounts(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client, "admin", "hunter2")
        response = client.post("/admin/users/999/quota", json={"quota_bytes": 1})

    assert response.status_code == 404
