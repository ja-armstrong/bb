"""Tests for the Admin Panel's VPN page backing: ticket #10.

The settings live in ``openvpn/vpn.env`` next to the OpenVPN config the
gluetun Subsystem mounts; a server change rewrites the config's server
lines; applying the change is a gluetun restart. The egress probe runs
from the API's shared (gluetun) network namespace, with the transport as
the injected seam — no real network here.
"""

import os
from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.main import COOKIE_NAME, create_app
from api.vpn import EGRESS_URL, VpnControl
from shared import db


def make_config(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "bitbeard.db"),
        service_token="service-token",
        admin_user="admin",
        admin_password="hunter2",
        default_quota_gb=50.0,
        openvpn_dir=str(tmp_path / "openvpn"),
        vpn_provider="",
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


OVPN_TEMPLATE = """client
dev tun
proto udp
remote 181.215.172.84 1194
resolv-retry infinite
verify-x509-name CN=us9723.nordvpn.com

<ca>
CERTIFICATE
</ca>
"""


def write_ovpn(openvpn_dir, text=OVPN_TEMPLATE):
    os.makedirs(openvpn_dir, exist_ok=True)
    path = os.path.join(openvpn_dir, "custom.ovpn")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class FakeEgress:
    """Canned probe answers, with every URL it was asked about."""

    def __init__(self, ip="203.0.113.7"):
        self.ip = ip
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.ip


def make_vpn(tmp_path, *, provider_fallback="", egress=None):
    return VpnControl(
        str(tmp_path / "openvpn"),
        provider_fallback=provider_fallback,
        egress_transport=egress,
    )


def make_client(config, vpn):
    app = create_app(config=config, vpn_control=vpn)
    return TestClient(app)


# --- The settings store (api.vpn) --------------------------------------------------


def test_read_settings_fall_back_to_the_compose_provider(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))

    settings = make_vpn(tmp_path, provider_fallback="nordvpn").read_settings()

    assert settings.provider == "nordvpn"
    assert settings.openvpn_user is None
    assert settings.openvpn_password is None
    assert settings.server == "181.215.172.84"  # parsed from the config


def test_update_settings_round_trips(tmp_path):
    ovpn = write_ovpn(str(tmp_path / "openvpn"))
    vpn = make_vpn(tmp_path)

    settings = vpn.update_settings(
        provider="mullvad",
        openvpn_user="sailor",
        openvpn_password="secret",
        server="de5.mullvad.net",
    )

    assert settings.provider == "mullvad"
    assert settings.openvpn_user == "sailor"
    assert settings.openvpn_password == "secret"
    assert settings.server == "de5.mullvad.net"

    # The settings file is the .env-style store of record.
    stored = read(os.path.join(str(tmp_path / "openvpn"), "vpn.env"))
    assert "VPN_PROVIDER=mullvad" in stored
    assert "OPENVPN_USER=sailor" in stored
    assert "OPENVPN_PASSWORD=secret" in stored
    assert "OPENVPN_SERVER=de5.mullvad.net" in stored

    # The OpenVPN config's server lines were rewritten, port preserved.
    config_text = read(ovpn)
    assert "remote de5.mullvad.net 1194" in config_text
    assert "verify-x509-name CN=de5.mullvad.net" in config_text
    assert "181.215.172.84" not in config_text
    assert "CERTIFICATE" in config_text  # the rest of the file survived


def test_update_without_a_server_leaves_the_config_alone(tmp_path):
    ovpn = write_ovpn(str(tmp_path / "openvpn"))
    before = read(ovpn)
    vpn = make_vpn(tmp_path)

    vpn.update_settings(provider="mullvad")

    assert read(ovpn) == before


def test_update_preserves_untouched_fields(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    vpn = make_vpn(tmp_path)
    vpn.update_settings(openvpn_user="sailor", openvpn_password="secret")

    settings = vpn.update_settings(provider="mullvad")

    assert settings.openvpn_user == "sailor"
    assert settings.openvpn_password == "secret"
    assert settings.provider == "mullvad"


def test_update_rewrites_a_config_without_a_remote_line(tmp_path):
    ovpn = write_ovpn(
        str(tmp_path / "openvpn"),
        text="client\ndev tun\n<ca>\nCERTIFICATE\n</ca>\n",
    )
    vpn = make_vpn(tmp_path)

    settings = vpn.update_settings(server="de5.mullvad.net")

    assert settings.server == "de5.mullvad.net"
    assert "remote de5.mullvad.net 1194" in read(ovpn)


def test_the_egress_probe_asks_the_ip_echo_service(tmp_path):
    egress = FakeEgress()
    vpn = make_vpn(tmp_path, egress=egress)

    assert vpn.egress_ip() == "203.0.113.7"
    assert egress.urls == [EGRESS_URL]


def test_a_failing_probe_reads_as_unknown(tmp_path):
    def broken_transport(url):
        raise RuntimeError("network down")

    vpn = make_vpn(tmp_path, egress=broken_transport)

    assert vpn.egress_ip() is None


# --- The API endpoints ----------------------------------------------------------------


def test_vpn_endpoints_require_a_session(tmp_path):
    config = make_config(tmp_path)
    vpn = make_vpn(tmp_path, egress=FakeEgress())

    with make_client(config, vpn) as client:
        assert client.get("/admin/vpn/status").status_code == 401
        assert client.post(
            "/admin/vpn/config", json={"provider": "mullvad"}
        ).status_code == 401
        assert client.post("/admin/vpn/test").status_code == 401


def test_vpn_endpoints_refuse_the_user_role(tmp_path):
    config = make_config(tmp_path)
    vpn = make_vpn(tmp_path, egress=FakeEgress())

    with make_client(config, vpn) as client:
        db.create_account(
            config.db_path, username="deckhand", password="ropes123", role="user"
        )
        _login(client, "deckhand", "ropes123")

        assert client.get("/admin/vpn/status").status_code == 403
        assert client.post(
            "/admin/vpn/config", json={"provider": "mullvad"}
        ).status_code == 403
        assert client.post("/admin/vpn/test").status_code == 403


def test_status_reports_provider_server_and_egress_ip(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    config = make_config(tmp_path, vpn_provider="nordvpn")
    egress = FakeEgress("198.51.100.9")
    vpn = make_vpn(tmp_path, provider_fallback="nordvpn", egress=egress)

    with make_client(config, vpn) as client:
        _login(client)
        response = client.get("/admin/vpn/status")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "nordvpn",
        "openvpn_user": None,
        "server": "181.215.172.84",
        "egress_ip": "198.51.100.9",
    }
    assert egress.urls == [EGRESS_URL]


def test_status_never_returns_the_password(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    config = make_config(tmp_path)
    vpn = make_vpn(tmp_path, egress=FakeEgress())
    vpn.update_settings(openvpn_user="sailor", openvpn_password="secret")

    with make_client(config, vpn) as client:
        _login(client)
        response = client.get("/admin/vpn/status")

    assert "secret" not in response.text
    assert response.json()["openvpn_user"] == "sailor"


def test_config_round_trip_writes_and_reads(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    config = make_config(tmp_path)
    vpn = make_vpn(tmp_path, egress=FakeEgress())

    with make_client(config, vpn) as client:
        _login(client)
        response = client.post(
            "/admin/vpn/config",
            json={
                "provider": "mullvad",
                "openvpn_user": "sailor",
                "openvpn_password": "secret",
                "server": "de5.mullvad.net",
            },
        )
        status = client.get("/admin/vpn/status")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mullvad"
    assert body["openvpn_user"] == "sailor"
    assert body["server"] == "de5.mullvad.net"
    assert "secret" not in response.text  # never echoed back

    assert status.json()["provider"] == "mullvad"
    assert status.json()["server"] == "de5.mullvad.net"
    assert "VPN_PROVIDER=mullvad" in read(
        os.path.join(str(tmp_path / "openvpn"), "vpn.env")
    )
    assert "remote de5.mullvad.net 1194" in read(
        os.path.join(str(tmp_path / "openvpn"), "custom.ovpn")
    )


def test_config_is_audited_without_the_password(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    config = make_config(tmp_path)
    vpn = make_vpn(tmp_path, egress=FakeEgress())

    with make_client(config, vpn) as client:
        _login(client)
        client.post(
            "/admin/vpn/config",
            json={"provider": "mullvad", "openvpn_password": "secret"},
        )

    rows = _audit_rows(config, "vpn_config_changed")
    assert len(rows) == 1
    assert rows[0]["actor"] == "admin"
    assert "mullvad" in rows[0]["details"]
    assert "secret" not in rows[0]["details"]


def test_config_refuses_provided_but_blank_fields(tmp_path):
    config = make_config(tmp_path)
    vpn = make_vpn(tmp_path, egress=FakeEgress())

    with make_client(config, vpn) as client:
        _login(client)
        response = client.post("/admin/vpn/config", json={"provider": "  "})

    assert response.status_code == 400
    assert not os.path.exists(
        os.path.join(str(tmp_path / "openvpn"), "vpn.env")
    )
    assert _audit_rows(config, "vpn_config_changed") == []


def test_test_endpoint_reports_the_egress_ip_and_audits(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    config = make_config(tmp_path, vpn_provider="nordvpn")
    vpn = make_vpn(
        tmp_path, provider_fallback="nordvpn", egress=FakeEgress("198.51.100.9")
    )

    with make_client(config, vpn) as client:
        _login(client)
        response = client.post("/admin/vpn/test")

    assert response.status_code == 200
    body = response.json()
    assert body["egress_ip"] == "198.51.100.9"
    assert body["provider"] == "nordvpn"

    rows = _audit_rows(config, "vpn_test")
    assert len(rows) == 1
    assert '"egress_ip": "198.51.100.9"' in rows[0]["details"]
    assert '"provider": "nordvpn"' in rows[0]["details"]


def test_a_failed_test_is_audited_with_an_unknown_ip(tmp_path):
    write_ovpn(str(tmp_path / "openvpn"))
    config = make_config(tmp_path)

    def broken_transport(url):
        return None

    vpn = make_vpn(tmp_path, egress=broken_transport)

    with make_client(config, vpn) as client:
        _login(client)
        response = client.post("/admin/vpn/test")

    assert response.status_code == 200
    assert response.json()["egress_ip"] is None
    rows = _audit_rows(config, "vpn_test")
    assert len(rows) == 1
    assert '"egress_ip": null' in rows[0]["details"]
