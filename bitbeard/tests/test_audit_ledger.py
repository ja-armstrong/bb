"""Tests for the Audit Ledger: ticket #4.

Every consequential action (logins success/fail, searches, download
submissions/cancels, Lifecycle Events) writes an Audit Ledger row with a
timestamp, Actor, action and details; the Admin Panel reads it through
``GET /admin/audit`` with Actor/action/date filters; and bot-proxied
actions are attributed to the acting Discord user — the linked Account,
or ``discord:<name>`` when unlinked.
"""

import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, MANAGER_ACTOR, create_app
from api.manager_proxy import ProxyResponse
from shared import db
from shared.security import hash_password


MANAGER_URL = "http://manager.test"


def make_config(db_path, **overrides):
    values = dict(
        db_path=str(db_path),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        manager_url=MANAGER_URL,
    )
    values.update(overrides)
    return Config(**values)


def _insert_account(config, username, password, role="user", discord_id=None):
    db.initialize(config.db_path)
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            """
            INSERT INTO users (username, password_hash, role, discord_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, hash_password(password), role, discord_id, db.utc_now()),
        )
        connection.commit()


def _login(client, username="admin", password="hunter2"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


def _fake_session(client, config, account_id=1):
    """Put a live Session in the store and present it, without logging in.

    Logging in through the endpoint writes a ``login`` Audit Ledger row;
    these tests seed the ledger themselves, so the Session is inserted
    directly to keep the counts exact. The bootstrap Admin is Account 1.
    """
    token = "fabricated-session-token"
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            """
            INSERT INTO sessions (token, account_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, account_id, db.utc_now(), "2099-01-01T00:00:00+00:00"),
        )
        connection.commit()
    client.cookies.set(COOKIE_NAME, token)
    return token


def _read_ledger(config):
    connection = sqlite3.connect(config.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM audit ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


class FakeTransport:
    """Answers proxied calls with canned responses, recording them."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, method, url, *, headers, params=None, json=None):
        self.calls.append({"method": method, "headers": headers})
        if self.responses:
            return self.responses.pop(0)
        return ProxyResponse(200, {})

    @property
    def last(self):
        return self.calls[-1]


SEARCH_RESULTS = {
    "results": [
        {"title": "Some Movie", "magnet_link": "magnet:?xt=urn:btih:x",
         "seeders": 10, "size_gb": 1.0}
    ]
}


# --- Rows for every consequential action ---------------------------------


def test_each_action_type_writes_a_ledger_row(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport([
        ProxyResponse(200, SEARCH_RESULTS),
        ProxyResponse(200, {}),
        ProxyResponse(200, {"cancelled": ["Some Movie"]}),
    ])

    with TestClient(create_app(config, proxy_transport=transport)) as client:
        client.post("/auth/login", json={"username": "admin", "password": "nope"})
        _login(client)
        client.get("/search", params={"q": "linux iso"})
        client.post(
            "/download",
            json={"magnet_link": "magnet:?xt=urn:btih:x", "category": "Movie"},
        )
        client.post("/cancel")
        for payload in (
            {"event": "submitted", "torrent_name": "Some Movie",
             "category": "Movie", "actor": "admin"},
            {"event": "completed", "torrent_name": "Some Movie",
             "category": "Movie", "actor": "admin"},
            {"event": "drained"},
        ):
            response = client.post(
                "/internal/events",
                json=payload,
                headers={"Authorization": "Bearer service-token"},
            )
            assert response.status_code == 200

    rows = _read_ledger(config)
    actions = [row["action"] for row in rows]
    assert {
        "login_failed", "login", "search", "download_submit",
        "download_cancel", "lifecycle_submitted", "lifecycle_completed",
        "lifecycle_drained",
    } <= set(actions)

    by_action = {row["action"]: row for row in rows}
    for action, row in by_action.items():
        # Every row records timestamp, Actor and details.
        assert row["ts"], f"{action} row has no timestamp"
        assert row["actor"], f"{action} row has no Actor"
        assert row["details"] is not None, f"{action} row has no details"
        assert json.loads(row["details"]) is not None

    assert by_action["login_failed"]["actor"] == "admin"
    assert json.loads(by_action["login_failed"]["details"])["reason"]
    assert by_action["login"]["actor"] == "admin"
    assert by_action["search"]["actor"] == "admin"
    assert json.loads(by_action["search"]["details"]) == {"query": "linux iso"}
    assert json.loads(by_action["download_submit"]["details"])["category"] == "Movie"
    assert json.loads(by_action["download_cancel"]["details"]) == {
        "cancelled": ["Some Movie"]
    }
    assert by_action["lifecycle_drained"]["actor"] == MANAGER_ACTOR


def test_login_rows_distinguish_success_from_failure(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        client.post("/auth/login", json={"username": "ghost", "password": "x"})
        _login(client)

    rows = _read_ledger(config)
    login_rows = [(row["action"], row["actor"]) for row in rows
                  if row["action"].startswith("login")]
    assert ("login_failed", "ghost") in login_rows
    assert ("login", "admin") in login_rows


# --- GET /admin/audit ------------------------------------------------------


def _seed_ledger(config, rows):
    db.initialize(config.db_path)
    with closing(db.connect(config.db_path)) as connection:
        for ts, actor, action in rows:
            connection.execute(
                "INSERT INTO audit (ts, actor, action, details) VALUES (?, ?, ?, ?)",
                (ts, actor, action, None),
            )
        connection.commit()


def test_admin_audit_requires_admin_role(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        _insert_account(config, "deckhand", "ropes123")
        _login(client, "deckhand", "ropes123")
        assert client.get("/admin/audit").status_code == 403

    with TestClient(create_app(config)) as fresh:
        assert fresh.get("/admin/audit").status_code == 401


def test_admin_audit_returns_entries_newest_first(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    _seed_ledger(config, [
        ("2026-11-13T10:00:00+00:00", "admin", "login"),
        ("2026-11-13T11:00:00+00:00", "deckhand", "search"),
    ])

    with TestClient(create_app(config)) as client:
        _fake_session(client, config)
        response = client.get("/admin/audit")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert [entry["action"] for entry in data["entries"]] == ["search", "login"]
    for entry in data["entries"]:
        assert entry["ts"]
        assert entry["actor"]


def test_admin_audit_filters_by_actor_action_and_date(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    _seed_ledger(config, [
        ("2026-11-11T09:00:00+00:00", "admin", "login"),
        ("2026-11-12T09:00:00+00:00", "deckhand", "search"),
        ("2026-11-12T18:00:00+00:00", "deckhand", "download_submit"),
        ("2026-11-13T09:00:00+00:00", "deckhand", "search"),
        ("2026-11-14T09:00:00+00:00", "admin", "download_submit"),
    ])

    with TestClient(create_app(config)) as client:
        _fake_session(client, config)

        by_actor = client.get("/admin/audit", params={"actor": "deckhand"}).json()
        assert by_actor["total"] == 3
        assert {entry["actor"] for entry in by_actor["entries"]} == {"deckhand"}

        by_action = client.get(
            "/admin/audit", params={"action": "search"}
        ).json()
        assert by_action["total"] == 2
        assert {entry["action"] for entry in by_action["entries"]} == {"search"}

        by_actor_and_action = client.get(
            "/admin/audit", params={"actor": "deckhand", "action": "search"}
        ).json()
        assert by_actor_and_action["total"] == 2

        one_day = client.get(
            "/admin/audit", params={"start": "2026-11-12", "end": "2026-11-12"}
        ).json()
        # The date-only bounds cover the whole day, bounds inclusive.
        assert one_day["total"] == 2
        assert {entry["action"] for entry in one_day["entries"]} == {
            "search", "download_submit"
        }

        wide = client.get(
            "/admin/audit", params={"start": "2026-11-12", "end": "2026-11-13"}
        ).json()
        assert wide["total"] == 3

        combined = client.get(
            "/admin/audit",
            params={"actor": "deckhand", "action": "search",
                    "start": "2026-11-13", "end": "2026-11-13"},
        ).json()
        assert combined["total"] == 1
        assert combined["entries"][0]["ts"].startswith("2026-11-13")


def test_admin_audit_paginates_with_the_total(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    _seed_ledger(config, [
        (f"2026-11-1{day}T09:00:00+00:00", "admin", "search")
        for day in range(1, 6)
    ])

    with TestClient(create_app(config)) as client:
        _fake_session(client, config)

        page = client.get("/admin/audit", params={"limit": 2, "offset": 2}).json()

        assert page["total"] == 5
        assert len(page["entries"]) == 2
        # Newest first, so the third page holds days 13 and 12.
        assert [entry["ts"][8:10] for entry in page["entries"]] == ["13", "12"]

        past_the_end = client.get(
            "/admin/audit", params={"limit": 2, "offset": 6}
        ).json()
        assert past_the_end["total"] == 5
        assert past_the_end["entries"] == []


# --- Bot-proxied attribution ------------------------------------------------


def test_bot_call_is_attributed_to_the_linked_account(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport([ProxyResponse(200, SEARCH_RESULTS)])

    with TestClient(create_app(config, proxy_transport=transport)) as client:
        _insert_account(config, "deckhand", "ropes123", discord_id="123456789")
        response = client.get(
            "/search",
            params={"q": "linux iso"},
            headers={
                "Authorization": "Bearer service-token",
                "X-Bitbeard-Discord": "123456789",
                "X-Bitbeard-Discord-Name": "Salty",
            },
        )

    assert response.status_code == 200
    # The manager is told which Account acted...
    assert transport.last["headers"]["X-Bitbeard-Account"] == "deckhand"
    # ...and the ledger names that Account, not the Bot.
    rows = [row for row in _read_ledger(config) if row["action"] == "search"]
    assert [(row["actor"], json.loads(row["details"])) for row in rows] == [
        ("deckhand", {"query": "linux iso", "via": "discord"})
    ]


def test_bot_call_for_an_unlinked_user_is_discord_name(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport()

    with TestClient(create_app(config, proxy_transport=transport)) as client:
        _insert_account(config, "deckhand", "ropes123", discord_id="123456789")
        _login(client)
        assert client.get(
            "/search",
            params={"q": "linux iso"},
            headers={
                "Authorization": "Bearer service-token",
                "X-Bitbeard-Discord": "987654321",
                "X-Bitbeard-Discord-Name": "Barnacle",
            },
        ).status_code == 200
        assert client.post(
            "/download",
            json={"magnet_link": "magnet:?xt=urn:btih:x", "category": "Movie"},
            headers={
                "Authorization": "Bearer service-token",
                "X-Bitbeard-Discord": "987654321",
                "X-Bitbeard-Discord-Name": "Barnacle",
            },
        ).status_code == 200

    rows = _read_ledger(config)
    assert [row["actor"] for row in rows if row["action"] == "search"] == [
        "discord:Barnacle"
    ]
    assert [row["actor"] for row in rows if row["action"] == "download_submit"] == [
        "discord:Barnacle"
    ]
    submit_details = json.loads(
        [row["details"] for row in rows if row["action"] == "download_submit"][0]
    )
    assert submit_details["via"] == "discord"

    # No Account to name: the manager attributes the lifecycle events to
    # the same discord:<name> Actor.
    download_call = transport.last
    assert download_call["headers"].get("X-Bitbeard-Account") is None
    assert download_call["headers"]["X-Bitbeard-Discord"] == "discord:Barnacle"


def test_bot_call_without_a_discord_header_is_refused(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport()

    with TestClient(create_app(config, proxy_transport=transport)) as client:
        response = client.get(
            "/search",
            params={"q": "x"},
            headers={"Authorization": "Bearer service-token"},
        )

    assert response.status_code == 401
    assert transport.calls == []


def test_bot_call_with_a_wrong_token_is_refused(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport()

    with TestClient(create_app(config, proxy_transport=transport)) as client:
        response = client.get(
            "/search",
            params={"q": "x"},
            headers={
                "Authorization": "Bearer wrong-token",
                "X-Bitbeard-Discord": "123",
            },
        )

    assert response.status_code == 401
    assert transport.calls == []


def test_bot_call_for_a_disabled_account_is_refused(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport()

    with TestClient(create_app(config, proxy_transport=transport)) as client:
        _insert_account(config, "marooned", "ropes123", discord_id="123456789")
        with closing(db.connect(config.db_path)) as connection:
            connection.execute(
                "UPDATE users SET disabled = 1 WHERE username = 'marooned'"
            )
            connection.commit()

        response = client.get(
            "/search",
            params={"q": "x"},
            headers={
                "Authorization": "Bearer service-token",
                "X-Bitbeard-Discord": "123456789",
            },
        )

    assert response.status_code == 401
    assert transport.calls == []


def test_bot_call_never_reaches_admin_endpoints(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        _insert_account(config, "deckhand", "ropes123", role="admin",
                        discord_id="123456789")
        response = client.get(
            "/admin/audit",
            headers={
                "Authorization": "Bearer service-token",
                "X-Bitbeard-Discord": "123456789",
            },
        )

    # Admin endpoints stay Session-only: the service path may resolve a
    # linked Admin Account, but a Frontend-service call without a Session
    # never reaches the Admin Panel.
    assert response.status_code == 401
