"""Tests for the Admin file endpoints on the API: ticket #10.

The API enforces the Admin role, writes the Audit Ledger rows *before*
forwarding (the spec's decision of record), and passes the manager's
deliberate 4xx answers through — it never touches files itself. The
manager transport is the injected seam; no HTTP and no filesystem here.
"""

from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, create_app
from api.manager_proxy import ProxyResponse
from shared import db


def make_config(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "bitbeard.db"),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        manager_url="http://manager.test",
    )
    values.update(overrides)
    return Config(**values)


def _login(client, username="admin", password="hunter2"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


def _audit_rows(config, action):
    with closing(db.connect(config.db_path)) as connection:
        return connection.execute(
            "SELECT actor, action, details FROM audit WHERE action = ?"
            " ORDER BY id",
            (action,),
        ).fetchall()


class FakeTransport:
    """Records every proxied call; answers with canned ProxyResponses."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.calls = []

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
        return ProxyResponse(self.status_code, self.payload)


def make_client(config, transport):
    app = create_app(config=config, proxy_transport=transport)
    return TestClient(app)


# --- Role gating -----------------------------------------------------------------


def test_file_endpoints_require_a_session(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with make_client(config, transport) as client:
        assert client.get("/admin/files").status_code == 401
        assert client.post(
            "/admin/files/delete", json={"path": "Movie"}
        ).status_code == 401

    assert transport.calls == []


def test_file_endpoints_refuse_the_user_role(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport()

    with make_client(config, transport) as client:
        db.create_account(
            config.db_path, username="deckhand", password="ropes123", role="user"
        )
        _login(client, "deckhand", "ropes123")

        assert client.get("/admin/files").status_code == 403
        assert client.post(
            "/admin/files/delete", json={"path": "Movie"}
        ).status_code == 403
        assert client.post(
            "/admin/files/rename", json={"path": "Movie", "new_name": "X"}
        ).status_code == 403
        assert client.post(
            "/admin/files/move",
            json={"path": "Movie", "target_dir": "TV Show"},
        ).status_code == 403

    assert transport.calls == []


# --- Browse (GET /admin/files) ----------------------------------------------------


def test_browse_forwards_the_path_to_the_manager(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(
        payload={"path": "Movie", "entries": [{"name": "file.bin"}]}
    )

    with make_client(config, transport) as client:
        _login(client)
        response = client.get("/admin/files?path=Movie")

    assert response.status_code == 200
    assert response.json() == {
        "path": "Movie",
        "entries": [{"name": "file.bin"}],
    }
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://manager.test/files"
    assert call["params"] == {"path": "Movie"}
    assert call["headers"]["Authorization"] == "Bearer service-token"


def test_browse_defaults_to_the_media_root(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(payload={"path": "", "entries": []})

    with make_client(config, transport) as client:
        _login(client)
        client.get("/admin/files")

    assert transport.calls[0]["params"] == {"path": ""}


def test_browse_of_an_unknown_path_passes_the_404_through(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(
        404, {"detail": "No file or directory at 'Missing'."}
    )

    with make_client(config, transport) as client:
        _login(client)
        response = client.get("/admin/files?path=Missing")

    assert response.status_code == 404
    assert "No file or directory" in response.json()["detail"]


def test_a_broken_manager_hop_is_one_502(tmp_path):
    config = make_config(tmp_path)

    class BrokenTransport:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("manager down")

    with make_client(config, BrokenTransport()) as client:
        _login(client)
        response = client.get("/admin/files")

    assert response.status_code == 502
    assert "unavailable" in response.json()["detail"].lower()


# --- Delete / rename / move (POST /admin/files/...) --------------------------------


def test_delete_is_audited_then_forwarded(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(payload={"deleted": "Movie/old"})

    with make_client(config, transport) as client:
        _login(client)
        response = client.post(
            "/admin/files/delete", json={"path": "Movie/old"}
        )

    assert response.status_code == 200
    assert response.json() == {"deleted": "Movie/old"}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://manager.test/files/delete"
    assert call["json"] == {"path": "Movie/old"}

    rows = _audit_rows(config, "file_delete")
    assert len(rows) == 1
    assert rows[0]["actor"] == "admin"
    assert '"op": "delete"' in rows[0]["details"]
    assert '"source": "Movie/old"' in rows[0]["details"]


def test_rename_is_audited_with_source_and_target_paths(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(payload={"renamed": "Movie/old", "to": "Movie/new"})

    with make_client(config, transport) as client:
        _login(client)
        response = client.post(
            "/admin/files/rename",
            json={"path": "Movie/old", "new_name": "new"},
        )

    assert response.status_code == 200
    call = transport.calls[0]
    assert call["json"] == {"path": "Movie/old", "new_name": "new"}

    rows = _audit_rows(config, "file_rename")
    assert len(rows) == 1
    assert '"op": "rename"' in rows[0]["details"]
    assert '"source": "Movie/old"' in rows[0]["details"]
    assert '"target": "Movie/new"' in rows[0]["details"]


def test_move_is_audited_with_source_and_target_paths(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(payload={"moved": "Movie/x", "to": "TV Show/x"})

    with make_client(config, transport) as client:
        _login(client)
        response = client.post(
            "/admin/files/move",
            json={"path": "Movie/x", "target_dir": "TV Show"},
        )

    assert response.status_code == 200
    call = transport.calls[0]
    assert call["json"] == {"path": "Movie/x", "target_dir": "TV Show"}

    rows = _audit_rows(config, "file_move")
    assert '"op": "move"' in rows[0]["details"]
    assert '"target": "TV Show/x"' in rows[0]["details"]


def test_a_refusal_is_passthrough_but_still_audited_first(tmp_path):
    """The spec: ledger rows are written *before* forwarding — a refused
    operation (Protected Path) is audited too, and the manager's answer
    surfaces verbatim."""
    config = make_config(tmp_path)
    transport = FakeTransport(
        409,
        {"detail": "'Movie/5' is inside an Active Torrent's save path."},
    )

    with make_client(config, transport) as client:
        _login(client)
        response = client.post(
            "/admin/files/delete", json={"path": "Movie/5"}
        )

    assert response.status_code == 409
    assert "Active Torrent" in response.json()["detail"]
    rows = _audit_rows(config, "file_delete")
    assert len(rows) == 1
    assert '"source": "Movie/5"' in rows[0]["details"]


def test_an_unknown_path_is_refused_with_the_managers_404(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport(
        404, {"detail": "No file or directory at 'Movie/Missing'."}
    )

    with make_client(config, transport) as client:
        _login(client)
        response = client.post(
            "/admin/files/delete", json={"path": "Movie/Missing"}
        )

    assert response.status_code == 404
    assert "No file or directory" in response.json()["detail"]


def test_the_forwarding_hop_failure_is_a_502(tmp_path):
    config = make_config(tmp_path)

    class BrokenTransport:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("manager down")

    with make_client(config, BrokenTransport()) as client:
        _login(client)
        response = client.post(
            "/admin/files/delete", json={"path": "Movie"}
        )

    assert response.status_code == 502
    # The attempt was still audited (rows are written before forwarding).
    assert len(_audit_rows(config, "file_delete")) == 1
