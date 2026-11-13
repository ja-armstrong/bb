"""Tests for the Link Code flow end to end (ticket #7).

A user generates a one-time Link Code in the Webui for a claimed Discord
ID; the binding is verified when the Bot next relays that Discord user's
ID on any Frontend-service call — audited, and one-time use. Until then
the Discord user stays an unlinked ``discord:<name>`` Actor.
"""

import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, create_app
from api.manager_proxy import ProxyResponse
from shared import db
from shared.security import hash_password


def make_config(db_path):
    return Config(
        db_path=str(db_path),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        manager_url="http://manager.test",
    )


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


def _bot_headers(discord_id, name="Salty"):
    return {
        "Authorization": "Bearer service-token",
        "X-Bitbeard-Discord": discord_id,
        "X-Bitbeard-Discord-Name": name,
    }


def _read_ledger(config):
    connection = sqlite3.connect(config.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute("SELECT * FROM audit ORDER BY id").fetchall()
    finally:
        connection.close()


class FakeTransport:
    def __call__(self, method, url, *, headers, params=None, json=None):
        return ProxyResponse(200, {"results": []})


def test_generating_a_link_code_requires_a_session(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config, proxy_transport=FakeTransport())) as client:
        response = client.post("/link/code", json={"discord_id": "987654321"})

    assert response.status_code == 401


def test_generating_a_link_code_returns_the_code_and_audits(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config, proxy_transport=FakeTransport())) as client:
        _login(client)
        response = client.post(
            "/link/code", json={"discord_id": "987654321"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["discord_id"] == "987654321"
    assert len(body["code"]) == 8

    # The code sits pending: nothing is linked yet.
    account = db.find_account(config.db_path, "admin")
    pending = db.pending_link_code(config.db_path, account["id"])
    assert pending["code"] == body["code"]

    rows = [
        row
        for row in _read_ledger(config)
        if row["action"] == "link_code_generated"
    ]
    assert [row["actor"] for row in rows] == ["admin"]
    assert json.loads(rows[0]["details"]) == {"discord_id": "987654321"}


def test_a_code_cannot_claim_an_id_another_account_already_holds(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config, proxy_transport=FakeTransport())) as client:
        _insert_account(
            config, "deckhand", "ropes123", discord_id="987654321"
        )
        _login(client)
        response = client.post(
            "/link/code", json={"discord_id": "987654321"}
        )

    assert response.status_code == 400
    assert "already linked" in response.json()["detail"]


def test_the_next_bot_interaction_verifies_the_binding(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config, proxy_transport=FakeTransport())) as client:
        _insert_account(config, "deckhand", "ropes123")
        _login(client, "deckhand", "ropes123")
        client.post("/link/code", json={"discord_id": "987654321"})

        # Generating the code binds nothing yet: another Discord user's
        # interaction stays an unlinked discord:<name> Actor.
        assert (
            client.get(
                "/search",
                params={"q": "x"},
                headers=_bot_headers("111222333", name="Barnacle"),
            ).status_code
            == 200
        )
        assert [
            row["actor"]
            for row in _read_ledger(config)
            if row["action"] == "search"
        ] == ["discord:Barnacle"]

        # The claimed Discord user interacts: the pending code is
        # verified against the reported ID and the Account is bound.
        with closing(db.connect(config.db_path)) as connection:
            connection.execute("DELETE FROM audit")
            connection.commit()
        assert (
            client.get(
                "/search",
                params={"q": "linux iso"},
                headers=_bot_headers("987654321"),
            ).status_code
            == 200
        )

        rows = _read_ledger(config)
        assert [
            (row["actor"], row["action"])
            for row in rows
            if row["action"] in ("discord_linked", "search")
        ] == [
            ("deckhand", "discord_linked"),
            ("deckhand", "search"),
        ]
        linked_row = [
            row for row in rows if row["action"] == "discord_linked"
        ][0]
        assert json.loads(linked_row["details"]) == {"discord_id": "987654321"}

        # One-time use: the code is spent, but the binding persists —
        # later interactions keep resolving to the Account, with no new
        # discord_linked row.
        account = db.find_account(config.db_path, "deckhand")
        assert db.pending_link_code(config.db_path, account["id"]) is None
        with closing(db.connect(config.db_path)) as connection:
            connection.execute("DELETE FROM audit WHERE action = 'search'")
            connection.commit()
        assert (
            client.get(
                "/search",
                params={"q": "more"},
                headers=_bot_headers("987654321"),
            ).status_code
            == 200
        )
        rows = _read_ledger(config)
        assert [
            row["action"] for row in rows if row["action"] == "search"
        ] == ["search"]
        assert [row["actor"] for row in rows if row["action"] == "search"] == [
            "deckhand"
        ]


def test_an_unverified_code_does_not_bind(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config, proxy_transport=FakeTransport())) as client:
        _insert_account(config, "deckhand", "ropes123")
        _login(client, "deckhand", "ropes123")
        client.post("/link/code", json={"discord_id": "987654321"})

        # A different Discord user interacting changes nothing: the
        # claimed ID never showed up.
        assert (
            client.get(
                "/search",
                params={"q": "x"},
                headers=_bot_headers("111222333", name="Barnacle"),
            ).status_code
            == 200
        )
        assert [
            row["actor"]
            for row in _read_ledger(config)
            if row["action"] == "search"
        ] == ["discord:Barnacle"]

        account = db.find_account(config.db_path, "deckhand")
        assert db.pending_link_code(config.db_path, account["id"]) is not None
        assert account["discord_id"] is None
