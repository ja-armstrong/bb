"""Tests for the Download Manager HTTP surface and service-token gate."""
from fastapi.testclient import TestClient

from manager.config import Config
from manager.main import create_app

TOKEN = "service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeManager:
    def __init__(self):
        self.submitted = []
        self.cancelled = False

    def submit(self, magnet_link, category, actor=None, record=None):
        self.submitted.append((magnet_link, category, actor))

    def progress(self):
        return []

    def cancel_all(self):
        self.cancelled = True
        return []


class FakeQbt:
    pass


def make_config(tmp_path):
    return Config(
        qbit_username="admin",
        qbit_password="hunter2",
        base_download_dir="/media/downloads",
        jackett_api_key="jackett-key",
        service_token=TOKEN,
        db_path=str(tmp_path / "bitbeard.db"),
        reconcile_interval=0.0,  # the reconciliation job is exercised directly
    )


def test_health_is_public(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "manager"}


def test_progress_requires_the_service_token(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())

    with TestClient(app) as client:
        response = client.get("/progress")

    assert response.status_code == 401


def test_wrong_service_token_is_rejected(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())

    with TestClient(app) as client:
        response = client.get(
            "/progress", headers={"Authorization": "Bearer wrong"}
        )

    assert response.status_code == 401


def test_progress_returns_manager_rows_with_a_valid_token(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())
    app.state.download_manager = FakeManager()

    with TestClient(app) as client:
        response = client.get("/progress", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"progress": []}


def test_download_endpoint_submits_to_the_manager(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())
    manager = FakeManager()
    app.state.download_manager = manager

    with TestClient(app) as client:
        response = client.post(
            "/download",
            headers=AUTH,
            json={"magnet_link": "magnet:?xt=urn:btih:abc", "category": "Movies"},
        )

    assert response.status_code == 200
    assert manager.submitted == [("magnet:?xt=urn:btih:abc", "Movies", None)]


def test_download_endpoint_forwards_trusted_actor_headers_to_the_manager(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())
    manager = FakeManager()
    app.state.download_manager = manager

    with TestClient(app) as client:
        response = client.post(
            "/download",
            headers={
                **AUTH,
                "X-Bitbeard-Account": "deckhand",
                "X-Bitbeard-Discord": "salty#1234",
            },
            json={"magnet_link": "magnet:?xt=urn:btih:abc", "category": "Movies"},
        )

    assert response.status_code == 200
    # The acting Account wins; the Discord name is the fallback.
    assert manager.submitted == [("magnet:?xt=urn:btih:abc", "Movies", "deckhand")]


def test_download_endpoint_falls_back_to_the_discord_name_as_actor(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())
    manager = FakeManager()
    app.state.download_manager = manager

    with TestClient(app) as client:
        response = client.post(
            "/download",
            headers={**AUTH, "X-Bitbeard-Discord": "salty#1234"},
            json={"magnet_link": "magnet:?xt=urn:btih:abc", "category": "Movies"},
        )

    assert response.status_code == 200
    assert manager.submitted == [("magnet:?xt=urn:btih:abc", "Movies", "salty#1234")]


def test_cancel_endpoint_asks_the_manager_to_cancel(tmp_path):
    app = create_app(config=make_config(tmp_path), qbt_client=FakeQbt())
    manager = FakeManager()
    app.state.download_manager = manager

    with TestClient(app) as client:
        response = client.post("/cancel", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"cancelled": []}
    assert manager.cancelled
