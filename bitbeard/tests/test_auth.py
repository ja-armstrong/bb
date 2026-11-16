"""Tests for authentication: login, Sessions, Roles and password change.

Each test maps to an acceptance criterion of ticket #2.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.config import Config, load_config
from api.main import COOKIE_NAME, create_app
from api.sessions import SessionStore
from shared import db
from shared.security import hash_password


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


def _set_env(monkeypatch, **overrides):
    values = {
        "DATABASE_PATH": "/data/bitbeard.db",
        "SERVICE_TOKEN": "service-token",
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "hunter2",
        "DEFAULT_QUOTA_GB": "50",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _insert_account(config, username, password, role="user", disabled=False):
    """Create an Account directly in the store.

    Account-creation endpoints arrive with the Admin Panel; the store is the
    same one the API seeds and owns.
    """
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            """
            INSERT INTO users (username, password_hash, role, disabled, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, hash_password(password), role, int(disabled), db.utc_now()),
        )
        connection.commit()


def _login(client, username="admin", password="hunter2"):
    """Log in through the API and return the issued Session token."""
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


class FakeClock:
    """A controllable stand-in for the wall clock."""

    def __init__(self):
        self._now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)

    def __call__(self):
        return self._now


# --- Config ------------------------------------------------------------


def test_session_ttl_has_a_default_and_is_configurable(monkeypatch):
    _set_env(monkeypatch)
    assert load_config().session_ttl_hours == 24.0

    _set_env(monkeypatch, SESSION_TTL_HOURS="6")
    assert load_config().session_ttl_hours == 6.0


def test_public_https_defaults_on_and_reads_the_common_spellings(monkeypatch):
    _set_env(monkeypatch)
    assert load_config().public_https is True

    _set_env(monkeypatch, PUBLIC_HTTPS="0")
    assert load_config().public_https is False

    _set_env(monkeypatch, PUBLIC_HTTPS="no")
    assert load_config().public_https is False

    _set_env(monkeypatch, PUBLIC_HTTPS="1")
    assert load_config().public_https is True


# --- POST /auth/login ---------------------------------------------------


def test_valid_login_sets_httponly_session_cookie_and_returns_role(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/auth/login", json={"username": "admin", "password": "hunter2"}
        )

    assert response.status_code == 200
    assert response.json() == {"role": "admin"}
    set_cookie = response.headers["set-cookie"]
    assert COOKIE_NAME in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "path=/" in set_cookie.lower()
    # Browser-side expiry matches the server-side one.
    assert "max-age=86400" in set_cookie.lower()


def test_the_default_config_marks_the_session_cookie_secure(tmp_path):
    """PUBLIC_HTTPS=1 (the default) means the stack is behind the HTTPS
    proxy, so the login cookie carries the Secure flag."""
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/auth/login", json={"username": "admin", "password": "hunter2"}
        )

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert "secure" in set_cookie.lower()
    # The rest of the cookie's posture is unchanged.
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_an_observed_https_request_is_secure_even_when_public_https_is_off(
    tmp_path,
):
    """X-Forwarded-Proto: https is the evidence the request really
    arrived over TLS — exactly where a Secure cookie belongs, whatever
    PUBLIC_HTTPS says."""
    config = make_config(tmp_path / "bitbeard.db", public_https=False)

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/auth/login",
            json={"username": "admin", "password": "hunter2"},
            headers={"X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_public_https_0_loses_the_secure_flag_over_plain_http(tmp_path):
    """The plain-HTTP escape hatch: browsers refuse to send Secure
    cookies over plain HTTP, so forcing it there breaks every login
    silently. No Secure flag."""
    config = make_config(tmp_path / "bitbeard.db", public_https=False)

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/auth/login", json={"username": "admin", "password": "hunter2"}
        )

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert "secure" not in set_cookie.lower()
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_wrong_password_is_rejected(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/auth/login", json={"username": "admin", "password": "wrong"}
        )

    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_unknown_username_is_rejected_indistinguishably_from_wrong_password(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = client.post(
            "/auth/login", json={"username": "ghost", "password": "whatever"}
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid username or password."}


def test_disabled_account_cannot_log_in(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        _insert_account(config, "marooned", "ropes123", disabled=True)
        response = client.post(
            "/auth/login", json={"username": "marooned", "password": "ropes123"}
        )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_login_attempts_are_recorded_in_the_audit_ledger(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        client.post("/auth/login", json={"username": "admin", "password": "nope"})
        _login(client)

    connection = sqlite3.connect(config.db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT actor, action FROM audit").fetchall()
    finally:
        connection.close()

    actions = {row["action"] for row in rows}
    assert {"login", "login_failed"} <= actions


# --- The API boundary ----------------------------------------------------


def test_requests_without_a_valid_session_are_refused(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/admin/ping").status_code == 401
        assert client.get("/auth/me").status_code == 401
        assert client.post("/auth/logout").status_code == 401
        assert client.post(
            "/auth/password",
            json={"current_password": "a", "new_password": "b"},
        ).status_code == 401


def test_forged_session_tokens_are_refused(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        client.cookies.set(COOKIE_NAME, "forged-token")
        response = client.get("/auth/me")

    assert response.status_code == 401


# --- Roles ----------------------------------------------------------------


def test_user_role_is_refused_on_admin_endpoints(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        _insert_account(config, "deckhand", "ropes123")
        _login(client, "deckhand", "ropes123")

        assert client.get("/admin/ping").status_code == 403
        # The Role still allows the Account's own endpoints.
        assert client.get("/auth/me").json() == {
            "username": "deckhand",
            "role": "user",
        }


def test_admin_role_passes_the_admin_gate(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        _login(client)

        response = client.get("/admin/ping")

    assert response.status_code == 200
    assert response.json() == {"message": "pong", "admin": "admin"}


# --- POST /auth/password ---------------------------------------------------


def test_password_change_requires_the_current_password(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    app = create_app(config)

    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/auth/password",
            json={"current_password": "wrong", "new_password": "plunder99"},
        )

    assert response.status_code == 400

    with TestClient(create_app(config)) as fresh:
        # The old password still works, so nothing was changed.
        assert fresh.post(
            "/auth/login", json={"username": "admin", "password": "hunter2"}
        ).status_code == 200


def test_password_change_updates_the_password_and_revokes_other_sessions(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    app = create_app(config)

    with TestClient(app) as client:
        _insert_account(config, "deckhand", "ropes123")
        _login(client, "deckhand", "ropes123")  # the browser that changes it

        with TestClient(app) as other:
            _login(other, "deckhand", "ropes123")  # an older browser

            response = client.post(
                "/auth/password",
                json={"current_password": "ropes123", "new_password": "plunder99"},
            )
            assert response.status_code == 200

            # The older browser's Session is revoked server-side...
            assert other.get("/auth/me").status_code == 401
            # ...while the browser that made the change stays logged in.
            assert client.get("/auth/me").status_code == 200

    with TestClient(create_app(config)) as fresh:
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "ropes123"}
        ).status_code == 401
        assert fresh.post(
            "/auth/login", json={"username": "deckhand", "password": "plunder99"}
        ).status_code == 200


# --- Revocation --------------------------------------------------------------


def test_logout_revokes_the_session_server_side(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        token = _login(client)
        assert client.post("/auth/logout").status_code == 200
        assert COOKIE_NAME not in client.cookies

        # Replay the old token: the Session is dead, not just forgotten.
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/auth/me").status_code == 401


def test_sessions_are_revocable_directly_in_the_store(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    store = SessionStore(config.db_path)

    with TestClient(create_app(config)) as client:
        token = _login(client)
        assert client.get("/auth/me").status_code == 200

        assert store.revoke(token) is True
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/auth/me").status_code == 401
        assert store.revoke(token) is False


def test_expired_sessions_are_refused_and_purged(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    clock = FakeClock()
    app = create_app(config)
    app.state.sessions = SessionStore(config.db_path, ttl_hours=1.0, now=clock)

    with TestClient(app) as client:
        token = _login(client)
        assert client.get("/auth/me").status_code == 200

        clock.advance(hours=2)

        assert client.get("/auth/me").status_code == 401

    # The expired row was deleted on sight.
    connection = sqlite3.connect(config.db_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE token = ?", (token,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 0
