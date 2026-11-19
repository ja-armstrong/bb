"""Tests for Admin Panel Account management: ticket #8.

An Admin creates Accounts (Role + Storage Quota: default/override/
unlimited), resets passwords, disables and re-enables, revokes Sessions,
and reads every Account's link state and usage-vs-cap from the Users
listing. Every action lands in the Audit Ledger; every endpoint refuses
the User Role at the boundary.
"""

from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, create_app
from api.manager_proxy import ProxyResponse
from api.sessions import SessionStore
from shared import db
from shared.security import verify_password


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


def _login(client, username="admin", password="hunter2"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


def _create(
    client, username, *, password="ropes123", role="user", quota_bytes=None
):
    return client.post(
        "/admin/users",
        json={
            "username": username,
            "password": password,
            "role": role,
            "quota_bytes": quota_bytes,
        },
    )


def _record_download(config, account_id, size_bytes):
    """Put a Download record in the store as the manager would leave it."""
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            "INSERT INTO downloads (account, name, category, save_path,"
            " size_bytes, status, submitted_at) VALUES (?, 'Some Movies',"
            " 'Movies', '/media/Movies/1', ?, 'completed', ?)",
            (account_id, size_bytes, db.utc_now()),
        )
        connection.commit()


def _audit_actions(config, action=None):
    with closing(db.connect(config.db_path)) as connection:
        if action is None:
            return connection.execute(
                "SELECT actor, action, details FROM audit ORDER BY id"
            ).fetchall()
        return connection.execute(
            "SELECT actor, action, details FROM audit WHERE action = ?"
            " ORDER BY id",
            (action,),
        ).fetchall()


def _admin_id(config):
    return db.find_account(config.db_path, "admin")["id"]


class FakeTransport:
    """Records every proxied call; answers with canned ProxyResponses."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, headers, params=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        return ProxyResponse(200, {"message": "Download started successfully"})


# --- Storage-layer helpers ---------------------------------------------------


def test_create_account_refuses_duplicates_in_the_store(tmp_path):
    config = make_config(tmp_path)
    db.initialize(config.db_path)

    account_id = db.create_account(
        config.db_path,
        username="deckhand",
        password="ropes123",
        role="user",
    )
    again = db.create_account(
        config.db_path, username="deckhand", password="other123", role="user"
    )

    assert account_id is not None
    assert again is None
    assert verify_password("ropes123",
                           db.get_account(config.db_path, account_id)["password_hash"])


def test_revoke_sessions_for_account_can_spare_one_token(tmp_path):
    config = make_config(tmp_path)
    db.initialize(config.db_path)
    account_id = db.create_account(
        config.db_path, username="deckhand", password="ropes123", role="user"
    )
    store = SessionStore(config.db_path)
    keeper = store.issue(account_id)
    store.issue(account_id)

    assert store.revoke_all_for_account(account_id, keep=keeper.token) == 1
    assert store.resolve(keeper.token) is not None
    assert store.revoke_all_for_account(account_id) == 1
    assert store.resolve(keeper.token) is None


def test_count_enabled_admins_counts_only_enabled_admins(tmp_path):
    config = make_config(tmp_path)
    db.initialize(config.db_path)
    db.create_account(
        config.db_path, username="mate", password="ropes123", role="admin"
    )
    sailor = db.create_account(
        config.db_path, username="sailor", password="ropes123", role="admin"
    )
    user = db.create_account(
        config.db_path, username="deckhand", password="ropes123", role="user"
    )

    assert db.count_enabled_admins(config.db_path) == 2
    db.set_disabled(config.db_path, sailor, True)
    assert db.count_enabled_admins(config.db_path) == 1
    db.set_disabled(config.db_path, user, True)
    # A disabled user-role Account never counted either way.
    assert db.count_enabled_admins(config.db_path) == 1


# --- Role gating --------------------------------------------------------------


def test_user_role_is_refused_on_every_account_endpoint(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        target = db.create_account(
            config.db_path, username="deckhand", password="ropes123",
            role="user",
        )
        _login(client, "deckhand", "ropes123")

        assert client.get("/admin/users").status_code == 403
        assert client.post(
            "/admin/users",
            json={"username": "x", "password": "plunder99", "role": "user"},
        ).status_code == 403
        assert client.post(
            f"/admin/users/{target}/reset",
            json={"new_password": "plunder99"},
        ).status_code == 403
        assert client.post(f"/admin/users/{target}/disable").status_code == 403
        assert client.post(f"/admin/users/{target}/enable").status_code == 403
        assert client.post(
            f"/admin/users/{target}/revoke_sessions"
        ).status_code == 403
        assert client.post(
            f"/admin/users/{target}/quota", json={"quota_bytes": 2048}
        ).status_code == 403
        # The Role still allows the Account's own endpoints.
        assert client.get("/auth/me").status_code == 200


def test_admin_endpoints_require_a_session(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        assert client.get("/admin/users").status_code == 401
        assert client.post(
            "/admin/users",
            json={"username": "x", "password": "plunder99", "role": "user"},
        ).status_code == 401
        assert client.post("/admin/users/1/reset").status_code == 401


# --- POST /admin/users (create) ------------------------------------------------


def test_admin_can_create_an_account_with_the_default_quota(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        response = _create(client, "deckhand")

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "deckhand"
    assert body["role"] == "user"
    assert body["quota"] == {
        "used_bytes": 0,
        "cap_bytes": int(0.001 * 1024 ** 3),
        "unlimited": False,
        "exempt": False,
        "override_bytes": None,
    }
    # The credentials work: the Account can log in.
    with TestClient(create_app(config)) as fresh:
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "ropes123"}
        ).status_code == 200
    rows = _audit_actions(config, "account_created")
    assert len(rows) == 1
    assert rows[0]["actor"] == "admin"
    assert '"username": "deckhand"' in rows[0]["details"]


def test_created_admin_accounts_pass_the_admin_gate(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        assert _create(client, "bosun", role="admin").status_code == 200

    with TestClient(create_app(config)) as fresh:
        _login(fresh, "bosun", "ropes123")
        assert fresh.get("/admin/users").status_code == 200


def test_duplicate_username_is_refused(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        assert _create(client, "deckhand").status_code == 200
        response = _create(client, "deckhand", password="other12345")

    assert response.status_code == 400
    assert "already taken" in response.json()["detail"]
    # Nothing half-made lingers: still exactly one deckhand.
    with closing(db.connect(config.db_path)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'deckhand'"
        ).fetchone()[0]
    assert count == 1


def test_short_passwords_are_refused(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        response = _create(client, "deckhand", password="short")

    assert response.status_code == 422
    assert db.find_account(config.db_path, "deckhand") is None


# --- Quota variants: reflected in the listing and enforced ---------------------


def test_listing_reports_default_override_and_unlimited_quotas(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        _create(client, "defaulted")
        _create(client, "capped", quota_bytes=4096)
        _create(client, "freebooter", quota_bytes=db.QUOTA_UNLIMITED)
        listing = client.get("/admin/users").json()

    by_name = {u["username"]: u for u in listing["users"]}
    assert listing["default_quota_bytes"] == int(0.001 * 1024 ** 3)
    assert by_name["defaulted"]["quota"]["cap_bytes"] == int(0.001 * 1024 ** 3)
    assert by_name["defaulted"]["quota"]["override_bytes"] is None
    assert by_name["capped"]["quota"]["cap_bytes"] == 4096
    assert by_name["capped"]["quota"]["override_bytes"] == 4096
    assert by_name["freebooter"]["quota"]["unlimited"] is True
    assert by_name["freebooter"]["quota"]["cap_bytes"] is None


def test_override_quotas_are_enforced_and_unlimited_is_not(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        _login(client)
        _create(client, "capped", quota_bytes=1024)
        _create(client, "freebooter", quota_bytes=db.QUOTA_UNLIMITED)

        _login(client, "capped", "ropes123")
        response = client.post(
            "/download",
            json={"magnet_link": "magnet:?xt=urn:btih:abcdef",
                  "category": "Movies", "size_bytes": 2048},
        )
        assert response.status_code == 403
        assert transport.calls == []

        _login(client, "freebooter", "ropes123")
        response = client.post(
            "/download",
            json={"magnet_link": "magnet:?xt=urn:btih:abcdef",
                  "category": "Movies", "size_bytes": 2048},
        )
        assert response.status_code == 200
        assert len(transport.calls) == 1


# --- POST /admin/users/{id}/reset ----------------------------------------------


def test_admin_can_reset_a_password_and_it_kills_existing_sessions(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config)

    with TestClient(app) as admin_browser:
        _login(admin_browser)
        account_id = _create(admin_browser, "deckhand").json()["id"]

        with TestClient(app) as user_browser:
            _login(user_browser, "deckhand", "ropes123")
            assert user_browser.get("/auth/me").status_code == 200

            response = admin_browser.post(
                f"/admin/users/{account_id}/reset",
                json={"new_password": "plunder99"},
            )
            assert response.status_code == 200
            assert response.json()["sessions_revoked"] == 1

            # The old browser's Session is revoked server-side...
            assert user_browser.get("/auth/me").status_code == 401

    # The old password is dead, the new one works.
    with TestClient(create_app(config)) as fresh:
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "ropes123"}
        ).status_code == 401
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "plunder99"}
        ).status_code == 200

    rows = _audit_actions(config, "password_reset")
    assert len(rows) == 1
    assert rows[0]["actor"] == "admin"
    # The new password itself never reaches the ledger.
    assert "plunder99" not in rows[0]["details"]


def test_reset_refuses_unknown_accounts(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        response = client.post(
            "/admin/users/999/reset", json={"new_password": "plunder99"}
        )

    assert response.status_code == 404


# --- Disable / enable with the last-Admin guard ---------------------------------


def test_disabling_an_account_blocks_login_and_kills_its_sessions(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config)

    with TestClient(app) as admin_browser:
        _login(admin_browser)
        account_id = _create(admin_browser, "deckhand").json()["id"]

        with TestClient(app) as user_browser:
            _login(user_browser, "deckhand", "ropes123")
            assert user_browser.get("/auth/me").status_code == 200

            response = admin_browser.post(f"/admin/users/{account_id}/disable")
            assert response.status_code == 200
            assert response.json()["disabled"] is True

            # The live Session dies immediately...
            assert user_browser.get("/auth/me").status_code == 401
            # ...and the Account cannot log back in.
            assert user_browser.post(
                "/auth/login", json={"username": "deckhand", "password": "ropes123"}
            ).status_code == 403

            reenabled = admin_browser.post(f"/admin/users/{account_id}/enable")
            assert reenabled.status_code == 200

    with TestClient(create_app(config)) as fresh:
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "ropes123"}
        ).status_code == 200

    actions = [row["action"] for row in _audit_actions(config)]
    assert {"account_disabled", "account_enabled"} <= set(actions)


def test_the_last_enabled_admin_cannot_be_disabled(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        response = client.post(f"/admin/users/{_admin_id(config)}/disable")

    assert response.status_code == 400
    assert "at least one admin" in response.json()["detail"]
    # The Admin is untouched and still holds their Session.
    assert db.count_enabled_admins(config.db_path) == 1
    assert client.get("/auth/me").status_code == 200
    assert _audit_actions(config, "account_disabled") == []


def test_the_guard_lifts_once_a_second_enabled_admin_exists(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        second = _create(client, "bosun", role="admin").json()["id"]

        # Two enabled Admins: either can be disabled...
        assert client.post(f"/admin/users/{second}/disable").status_code == 200
        # ...but the remaining one still cannot.
        response = client.post(f"/admin/users/{_admin_id(config)}/disable")
        assert response.status_code == 400
        assert "at least one admin" in response.json()["detail"]


def test_disabling_and_enabling_twice_are_refused(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        account_id = _create(client, "deckhand").json()["id"]
        assert client.post(f"/admin/users/{account_id}/disable").status_code == 200
        assert client.post(f"/admin/users/{account_id}/disable").status_code == 400
        assert client.post(f"/admin/users/{account_id}/enable").status_code == 200
        assert client.post(f"/admin/users/{account_id}/enable").status_code == 400


# --- POST /admin/users/{id}/revoke_sessions --------------------------------------


def test_revoking_sessions_kills_them_immediately(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config)

    with TestClient(app) as admin_browser:
        _login(admin_browser)
        account_id = _create(admin_browser, "deckhand").json()["id"]

        with TestClient(app) as first_browser:
            with TestClient(app) as second_browser:
                _login(first_browser, "deckhand", "ropes123")
                _login(second_browser, "deckhand", "ropes123")
                assert first_browser.get("/auth/me").status_code == 200
                assert second_browser.get("/auth/me").status_code == 200

                response = admin_browser.post(
                    f"/admin/users/{account_id}/revoke_sessions"
                )
                assert response.status_code == 200
                assert response.json()["sessions_revoked"] == 2

                assert first_browser.get("/auth/me").status_code == 401
                assert second_browser.get("/auth/me").status_code == 401

    # The Account itself is untouched: it can simply log in again.
    with TestClient(create_app(config)) as fresh:
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "ropes123"}
        ).status_code == 200

    rows = _audit_actions(config, "sessions_revoked")
    assert len(rows) == 1
    assert rows[0]["actor"] == "admin"
    assert '"revoked": 2' in rows[0]["details"]


# --- GET /admin/users (the Users listing) -----------------------------------------


def test_the_users_listing_reports_link_state_usage_and_disabled(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        linked = _create(client, "linked", quota_bytes=4096).json()["id"]
        away = _create(client, "away").json()["id"]

        # The Account generates a Link Code in the Webui; the Admin sees
        # the pending code and the ID it claims.
        _login(client, "linked", "ropes123")
        code = client.post(
            "/link/code", json={"discord_id": "salty#1234"}
        ).json()["code"]
        _record_download(config, linked, 1024)

        _login(client)
        client.post(f"/admin/users/{away}/disable")
        listing = client.get("/admin/users").json()

    by_name = {u["username"]: u for u in listing["users"]}
    assert by_name["admin"]["role"] == "admin"
    assert by_name["admin"]["quota"]["exempt"] is True
    assert by_name["admin"]["link_code"] is None

    linked_row = by_name["linked"]
    assert linked_row["disabled"] is False
    assert linked_row["discord_id"] is None
    assert linked_row["link_code"]["code"] == code
    assert linked_row["link_code"]["discord_id"] == "salty#1234"
    assert linked_row["quota"]["cap_bytes"] == 4096
    assert linked_row["quota"]["used_bytes"] == 1024

    away_row = by_name["away"]
    assert away_row["disabled"] is True
    assert away_row["link_code"] is None


def test_a_completed_discord_link_shows_as_linked_not_pending(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        account_id = _create(client, "linked").json()["id"]
        _login(client, "linked", "ropes123")
        client.post("/link/code", json={"discord_id": "salty#1234"})

        # The Bot's next interaction spends the code and binds the ID.
        client.headers["Authorization"] = "Bearer service-token"
        client.headers["X-Bitbeard-Discord"] = "salty#1234"
        client.get("/search?q=arrr")
        client.headers.pop("Authorization")
        client.headers.pop("X-Bitbeard-Discord")

        _login(client)
        row = next(
            u for u in client.get("/admin/users").json()["users"]
            if u["id"] == account_id
        )

    assert row["discord_id"] == "salty#1234"
    assert row["link_code"] is None


# --- Every admin action lands in the Audit Ledger ---------------------------------


def test_every_admin_action_is_audited_with_the_admin_as_actor(tmp_path):
    config = make_config(tmp_path)

    with TestClient(create_app(config)) as client:
        _login(client)
        account_id = _create(client, "deckhand").json()["id"]
        client.post(
            f"/admin/users/{account_id}/reset",
            json={"new_password": "plunder99"},
        )
        client.post(f"/admin/users/{account_id}/disable")
        client.post(f"/admin/users/{account_id}/enable")
        client.post(f"/admin/users/{account_id}/revoke_sessions")
        client.post(
            f"/admin/users/{account_id}/quota", json={"quota_bytes": 2048}
        )

    actions = {(row["actor"], row["action"])
               for row in _audit_actions(config)}
    expected = {
        "account_created", "password_reset", "account_disabled",
        "account_enabled", "sessions_revoked", "quota_assigned",
    }
    seen = {action for actor, action in actions if actor == "admin"}
    assert expected <= seen
    # No admin action was attributed to anyone else.
    for actor, action in actions:
        if action in expected:
            assert actor == "admin"
