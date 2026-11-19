"""Tests for the API's proxy to the Download Manager and /internal/events.

Each test maps to an acceptance criterion of ticket #3: the core user path
(Search, Download submission, progress, cancel) flows Frontend -> API ->
Download Manager with the Actor riding along, and Lifecycle Events land in
the Audit Ledger.
"""

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


def _insert_account(config, username, password, role="user"):
    with closing(db.connect(config.db_path)) as connection:
        connection.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, hash_password(password), role, db.utc_now()),
        )
        connection.commit()


def _login(client, username="admin", password="hunter2"):
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
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return ProxyResponse(200, {})

    @property
    def last(self):
        return self.calls[-1]


class FailingTransport:
    """Stands in for a manager that cannot be reached at all."""

    def __call__(self, method, url, **kwargs):
        raise ConnectionError("manager down")


SEARCH_RESULTS = {
    "results": [
        {
            "title": "Ubuntu 24.04 LTS Desktop amd64",
            "magnet_link": "magnet:?xt=urn:btih:abcdef",
            "seeders": 87,
            "size_gb": 3.42,
        }
    ]
}


def make_app(config, transport):
    return create_app(config, proxy_transport=transport)


def test_proxied_search_returns_manager_results_and_records_the_actor(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport([ProxyResponse(200, SEARCH_RESULTS)])

    with TestClient(make_app(config, transport)) as client:
        _login(client)
        response = client.get("/search", params={"q": "linux iso"})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["title"] == SEARCH_RESULTS["results"][0]["title"]
    assert result["seeders"] == 87
    assert result["magnet_link"].startswith("magnet:")
    assert "size_gb" in result

    call = transport.last
    assert call["method"] == "GET"
    assert call["url"] == f"{MANAGER_URL}/search"
    assert call["params"] == {"q": "linux iso"}
    assert call["headers"]["Authorization"] == "Bearer service-token"
    assert call["headers"]["X-Bitbeard-Account"] == "admin"

    with closing(db.connect(config.db_path)) as connection:
        rows = connection.execute(
            "SELECT actor, action, details FROM audit WHERE action = 'search'"
        ).fetchall()
    assert [(row["actor"], row["details"]) for row in rows] == [
        ("admin", '{"query": "linux iso"}')
    ]


def test_proxied_download_reaches_the_manager_with_the_actor(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport()

    with TestClient(make_app(config, transport)) as client:
        _login(client)
        response = client.post(
            "/download",
            json={
                "magnet_link": "magnet:?xt=urn:btih:abcdef",
                "category": "Movies",
            },
        )

    assert response.status_code == 200
    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == f"{MANAGER_URL}/download"
    assert call["json"] == {
        "magnet_link": "magnet:?xt=urn:btih:abcdef",
        "category": "Movies",
        "size_bytes": 0,
        "title": None,
    }
    assert call["headers"]["X-Bitbeard-Account"] == "admin"

    with closing(db.connect(config.db_path)) as connection:
        rows = connection.execute(
            "SELECT actor, action FROM audit WHERE action = 'download_submit'"
        ).fetchall()
    assert [row["actor"] for row in rows] == ["admin"]


def test_proxied_download_forwards_a_linked_discord_id(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    db.initialize(config.db_path)  # create the store; the bootstrap Admin is
    # not seeded because we insert our own Account first.
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, discord_id, created_at)
            VALUES (?, ?, 'admin', ?, ?)
            """,
            ("admin", hash_password("hunter2"), "salty#1234", db.utc_now()),
        )
        connection.commit()

    transport = FakeTransport()
    with TestClient(make_app(config, transport)) as client:
        _login(client)
        client.post(
            "/download",
            json={"magnet_link": "magnet:?xt=urn:btih:x", "category": "Other"},
        )

    assert transport.last["headers"]["X-Bitbeard-Discord"] == "salty#1234"


def test_progress_is_visible_through_the_api(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    manager_progress = {
        "progress": [
            {"name": "Some Show", "progress": "12.34%", "eta": "1h:2m:3s"}
        ]
    }
    transport = FakeTransport([ProxyResponse(200, manager_progress)])

    with TestClient(make_app(config, transport)) as client:
        _login(client)
        response = client.get("/progress")

    assert response.status_code == 200
    assert response.json() == manager_progress
    assert transport.last["url"] == f"{MANAGER_URL}/progress"


def test_cancel_is_proxied_and_audited(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport([ProxyResponse(200, {"cancelled": ["Some Show"]})])

    with TestClient(make_app(config, transport)) as client:
        _login(client)
        response = client.post("/cancel")

    assert response.status_code == 200
    assert response.json() == {"cancelled": ["Some Show"]}
    assert transport.last["method"] == "POST"

    with closing(db.connect(config.db_path)) as connection:
        rows = connection.execute(
            "SELECT actor, action FROM audit WHERE action = 'download_cancel'"
        ).fetchall()
    assert [row["actor"] for row in rows] == ["admin"]


def test_proxied_calls_require_a_session(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport()

    with TestClient(make_app(config, transport)) as client:
        assert client.get("/search", params={"q": "x"}).status_code == 401
        assert client.post(
            "/download", json={"magnet_link": "m", "category": "Movies"}
        ).status_code == 401
        assert client.get("/progress").status_code == 401
        assert client.post("/cancel").status_code == 401

    assert transport.calls == []


def test_user_role_can_use_the_provided_endpoints(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport([ProxyResponse(200, SEARCH_RESULTS)])

    with TestClient(make_app(config, transport)) as client:
        _insert_account(config, "deckhand", "ropes123")
        _login(client, "deckhand", "ropes123")
        assert client.get("/search", params={"q": "x"}).status_code == 200
        assert client.get("/progress").status_code == 200


def test_unreachable_manager_becomes_a_502(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(make_app(config, FailingTransport())) as client:
        _login(client)
        response = client.get("/search", params={"q": "linux iso"})

    assert response.status_code == 502
    assert response.json()["detail"] == "Download Manager unavailable."


def test_manager_error_becomes_a_502(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")
    transport = FakeTransport([ProxyResponse(500, {"detail": "boom"})])

    with TestClient(make_app(config, transport)) as client:
        _login(client)
        response = client.post("/cancel")

    assert response.status_code == 502


# --- POST /internal/events -------------------------------------------------


def _post_event(client, payload, token="service-token"):
    return client.post(
        "/internal/events",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_internal_events_requires_the_service_token(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert client.post("/internal/events", json={}).status_code == 401
        response = _post_event(
            client,
            {"event": "drained"},
            token="wrong-token",
        )

    assert response.status_code == 401


def test_internal_events_records_lifecycle_events_with_the_actor(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        assert _post_event(client, {
            "event": "submitted",
            "torrent_name": "Some Movies",
            "category": "Movies",
            "actor": "deckhand",
        }).status_code == 200
        assert _post_event(client, {
            "event": "completed",
            "torrent_name": "Some Movies",
            "category": "Movies",
            "actor": "deckhand",
        }).status_code == 200
        assert _post_event(client, {"event": "drained"}).status_code == 200

    with closing(db.connect(config.db_path)) as connection:
        rows = connection.execute(
            "SELECT actor, action FROM audit ORDER BY id"
        ).fetchall()
    actions = [(row["actor"], row["action"]) for row in rows]
    assert ("deckhand", "lifecycle_submitted") in actions
    assert ("deckhand", "lifecycle_completed") in actions
    assert (MANAGER_ACTOR, "lifecycle_drained") in actions


def test_internal_events_rejects_unknown_event_names(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)) as client:
        response = _post_event(client, {"event": "exploded"})

    assert response.status_code == 422
