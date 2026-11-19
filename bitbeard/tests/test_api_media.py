"""Tests for the API's My Media surface (ticket #6).

GET /media and POST /media/{id}/delete|rename|move|cancel: ownership and
role enforced at the API boundary, the acting Actor forwarded to the
manager as trusted headers, the manager's user-facing refusals passed
through, and every consequential operation written to the Audit Ledger.
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
        default_quota_gb=50.0,
        manager_url=MANAGER_URL,
    )
    values.update(overrides)
    return Config(**values)


def _insert_account(config, username, password="ropes123", role="user",
                    discord_id=None):
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, role, discord_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, hash_password(password), role, discord_id, db.utc_now()),
        )
        connection.commit()
        return cursor.lastrowid


def _record_download(config, account_id=None, discord_name=None, status="completed",
                     name="Some Movies", size_bytes=1024):
    """Put a Download record in the store as the manager would leave it."""
    with closing(db.connect(config.db_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO downloads (account, discord_name, name, category,"
            " save_path, size_bytes, status, submitted_at)"
            " VALUES (?, ?, ?, 'Movies', '/media/Movies/1', ?, ?, ?)",
            (account_id, discord_name, name, size_bytes, status, db.utc_now()),
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
        return ProxyResponse(200, {})

    @property
    def last(self):
        return self.calls[-1]


def _audit(config, action):
    with closing(db.connect(config.db_path)) as connection:
        return connection.execute(
            "SELECT actor, details FROM audit WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()


# --- GET /media ----------------------------------------------------------------


def test_media_lists_the_calling_actors_downloads(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([
        ProxyResponse(200, {"downloads": [{"id": 7, "name": "Some Movies"}]})
    ])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        _login(client, "deckhand")
        response = client.get("/media")

    assert response.status_code == 200
    assert response.json() == {"downloads": [{"id": 7, "name": "Some Movies"}]}
    # The listing is scoped to the calling Actor at the manager.
    assert transport.last["method"] == "GET"
    assert transport.last["url"] == f"{MANAGER_URL}/media"
    assert transport.last["headers"]["X-Bitbeard-Account-Id"] == str(account_id)


def test_media_requires_a_session(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        assert client.get("/media").status_code == 401

    assert transport.calls == []


def test_media_lists_unlinked_discord_actors_by_their_name(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([ProxyResponse(200, {"downloads": []})])

    with TestClient(create_app(config, transport)) as client:
        client.headers["Authorization"] = "Bearer service-token"
        client.headers["X-Bitbeard-Discord"] = "salty#1234"
        client.headers["X-Bitbeard-Discord-Name"] = "salty"
        client.get("/media")

    headers = transport.last["headers"]
    assert headers["X-Bitbeard-Discord"] == "discord:salty"
    assert "X-Bitbeard-Account-Id" not in headers


# --- Ownership at the API boundary ----------------------------------------------


def test_delete_of_another_actors_download_is_refused_before_the_manager(
    tmp_path,
):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        owner = _insert_account(config, "matey")
        download_id = _record_download(config, account_id=owner)
        _insert_account(config, "deckhand")
        _login(client, "deckhand")
        response = client.post(f"/media/{download_id}/delete")

    assert response.status_code == 403
    assert "another Actor" in response.json()["detail"]
    # The Download never reached the manager.
    assert transport.calls == []


def test_delete_of_an_unknown_download_is_a_404_at_the_boundary(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        _insert_account(config, "deckhand")
        _login(client, "deckhand")
        response = client.post("/media/999/delete")

    assert response.status_code == 404
    assert transport.calls == []


def test_the_owner_can_delete_and_it_is_audited(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([ProxyResponse(200, {"deleted": 5, "name": "x"})])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(config, account_id=account_id)
        _login(client, "deckhand")
        response = client.post(f"/media/{download_id}/delete")

    assert response.status_code == 200
    assert response.json() == {"deleted": 5, "name": "x"}
    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == f"{MANAGER_URL}/media/{download_id}/delete"
    assert call["headers"]["X-Bitbeard-Account"] == "deckhand"
    assert call["headers"]["X-Bitbeard-Account-Id"] == str(account_id)
    rows = _audit(config, "media_delete")
    assert rows[0]["actor"] == "deckhand"
    assert f'"download_id": {download_id}' in rows[0]["details"]
    assert rows[0]["details"].count("save_path") == 1


def test_an_admin_is_a_user_on_the_my_media_endpoints(tmp_path):
    """No admin passthrough: another Actor's media is the file manager's job."""
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        owner = _insert_account(config, "matey")
        download_id = _record_download(config, account_id=owner)
        _login(client, "admin", "hunter2")
        response = client.post(f"/media/{download_id}/delete")

    assert response.status_code == 403
    assert transport.calls == []


# --- Rename / move / cancel proxies ---------------------------------------------


def test_rename_is_proxied_with_the_new_name_and_audited(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([
        ProxyResponse(200, {"renamed": 5, "name": "Pirates Yarn"})
    ])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(config, account_id=account_id)
        _login(client, "deckhand")
        response = client.post(
            f"/media/{download_id}/rename", json={"name": "Pirates Yarn"}
        )

    assert response.status_code == 200
    assert transport.last["json"] == {"name": "Pirates Yarn"}
    rows = _audit(config, "media_rename")
    assert rows[0]["actor"] == "deckhand"
    assert '"new_name": "Pirates Yarn"' in rows[0]["details"]


def test_move_is_proxied_with_the_category_and_audited(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([
        ProxyResponse(200, {"moved": 5, "category": "Shows"})
    ])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(config, account_id=account_id)
        _login(client, "deckhand")
        response = client.post(
            f"/media/{download_id}/move", json={"category": "Shows"}
        )

    assert response.status_code == 200
    assert transport.last["json"] == {"category": "Shows"}
    rows = _audit(config, "media_move")
    assert rows[0]["actor"] == "deckhand"
    assert '"category": "Shows"' in rows[0]["details"]


def test_cancel_is_proxied_per_download_and_audited(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([ProxyResponse(200, {"cancelled": ["Some Movies"]})])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(
            config, account_id=account_id, status="in_flight"
        )
        _login(client, "deckhand")
        response = client.post(f"/media/{download_id}/cancel")

    assert response.status_code == 200
    assert transport.last["url"] == f"{MANAGER_URL}/media/{download_id}/cancel"
    rows = _audit(config, "download_cancel")
    assert rows[0]["actor"] == "deckhand"
    assert f'"download_id": {download_id}' in rows[0]["details"]


# --- The manager's refusals surface verbatim -------------------------------------


def test_an_active_torrent_refusal_passes_through_as_a_409(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([
        ProxyResponse(
            409,
            {"detail": "/media/Movies/1 belongs to an Active Torrent."},
        )
    ])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(config, account_id=account_id)
        _login(client, "deckhand")
        response = client.post(f"/media/{download_id}/delete")

    assert response.status_code == 409
    assert "Active Torrent" in response.json()["detail"]


def test_a_path_escape_refusal_passes_through_as_a_400(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([
        ProxyResponse(400, {"detail": "/evil escapes the Media Root."})
    ])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(config, account_id=account_id)
        _login(client, "deckhand")
        response = client.post(
            f"/media/{download_id}/rename", json={"name": "../escape"}
        )

    assert response.status_code == 400
    assert "escapes the Media Root" in response.json()["detail"]


def test_a_crashing_manager_is_still_a_502(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([ProxyResponse(500, {"detail": "boom"})])

    with TestClient(create_app(config, transport)) as client:
        account_id = _insert_account(config, "deckhand")
        download_id = _record_download(config, account_id=account_id)
        _login(client, "deckhand")
        response = client.post(f"/media/{download_id}/delete")

    assert response.status_code == 502
    assert response.json()["detail"] == "Download Manager unavailable."


def test_an_unlinked_discord_actor_manages_their_own_downloads(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([ProxyResponse(200, {"deleted": 5})])

    with TestClient(create_app(config, transport)) as client:
        _record_download(config, discord_name="discord:salty")
        client.headers["Authorization"] = "Bearer service-token"
        client.headers["X-Bitbeard-Discord"] = "salty#1234"
        client.headers["X-Bitbeard-Discord-Name"] = "salty"
        response = client.post("/media/1/delete")

    assert response.status_code == 200
    assert transport.last["headers"]["X-Bitbeard-Discord"] == "discord:salty"


def test_an_unlinked_discord_actor_cannot_touch_account_downloads(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with TestClient(create_app(config, transport)) as client:
        owner = _insert_account(config, "deckhand")
        _record_download(config, account_id=owner)
        client.headers["Authorization"] = "Bearer service-token"
        client.headers["X-Bitbeard-Discord"] = "salty#1234"
        client.headers["X-Bitbeard-Discord-Name"] = "salty"
        response = client.post("/media/1/delete")

    assert response.status_code == 403
    assert transport.calls == []
