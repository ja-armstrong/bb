"""Tests for the API service: config, startup and Admin bootstrap."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.config import Config, load_config
from api.main import create_app


def make_config(db_path):
    return Config(
        db_path=str(db_path),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
    )


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


def test_load_config_reads_every_variable(monkeypatch, tmp_path):
    _set_env(monkeypatch)

    config = load_config()

    assert config.db_path == "/data/bitbeard.db"
    assert config.service_token == "service-token"
    assert config.admin_user == "admin"
    assert config.admin_password == "hunter2"
    assert config.default_quota_gb == 50.0


def test_database_path_and_quota_have_defaults(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("DATABASE_PATH")
    monkeypatch.delenv("DEFAULT_QUOTA_GB")

    config = load_config()

    assert config.db_path == "/data/bitbeard.db"
    assert config.default_quota_gb == 50.0


def test_manager_url_has_a_default(monkeypatch):
    _set_env(monkeypatch)

    assert load_config().manager_url == "http://127.0.0.1:9871"


def test_default_quota_is_exposed_in_bytes(monkeypatch):
    _set_env(monkeypatch, DEFAULT_QUOTA_GB="2")

    config = load_config()

    assert config.default_quota_bytes == 2 * 1024 ** 3


@pytest.mark.parametrize("missing", ["SERVICE_TOKEN", "ADMIN_USER", "ADMIN_PASSWORD"])
def test_missing_required_variable_raises_naming_it(monkeypatch, missing):
    _set_env(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        load_config()


def test_non_numeric_quota_raises(monkeypatch):
    _set_env(monkeypatch, DEFAULT_QUOTA_GB="lots")

    with pytest.raises(ValueError):
        load_config()


def test_health_endpoint_reports_ok(tmp_path):
    app = create_app(make_config(tmp_path / "bitbeard.db"))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_startup_creates_database_with_schema_and_bootstrap_admin(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)):
        pass

    connection = sqlite3.connect(config.db_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        admin = connection.execute(
            "SELECT * FROM users WHERE username = 'admin'"
        ).fetchone()
    finally:
        connection.close()

    assert {"users", "sessions", "audit", "downloads"} <= tables
    assert admin is not None
    assert admin["role"] == "admin"


def test_restarting_the_api_does_not_reseed_the_admin(tmp_path):
    config = make_config(tmp_path / "bitbeard.db")

    with TestClient(create_app(config)):
        pass
    with TestClient(create_app(config)):
        pass

    connection = sqlite3.connect(config.db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        connection.close()

    assert count == 1
