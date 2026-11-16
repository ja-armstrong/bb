"""Tests for the Admin Panel's Subsystems page: ticket #9.

An Admin sees every Subsystem's container state and port probe, restarts/
stops/starts Subsystems, and disables/enables the Optional Subsystem (the
Bot) only. Docker actions are label-filtered to this compose project,
refuse unknown names, and every action lands in the Audit Ledger. The
docker socket is never touched here: a fake client rides the same seam the
real docker SDK client does.
"""

from contextlib import closing

from fastapi.testclient import TestClient

from api.config import Config
from api.docker_control import (
    COMPOSE_PROJECT_LABEL,
    DockerControl,
    OPTIONAL_SUBSYSTEMS,
    subsystem_ports,
)
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
        compose_project="bitbeard",
    )
    values.update(overrides)
    return Config(**values)


def _login(client, username="admin", password="hunter2"):
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


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


def _subsystem_rows(config):
    """The ledger's Subsystem rows (logins excluded)."""
    with closing(db.connect(config.db_path)) as connection:
        return connection.execute(
            "SELECT actor, action, details FROM audit"
            " WHERE action LIKE 'subsystem%' ORDER BY id"
        ).fetchall()


# --- Fake docker client -------------------------------------------------------
#
# Shaped like the docker SDK client the default factory builds:
# client.containers.list(all=..., filters={"label": "<key>=<value>"})
# returning objects with .name/.status and .restart()/.stop()/.start().


class FakeContainer:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status
        self.calls = []

    def restart(self):
        self.calls.append("restart")
        self.status = "running"

    def stop(self):
        self.calls.append("stop")
        self.status = "exited"

    def start(self):
        self.calls.append("start")
        self.status = "running"


class FakeContainers:
    def __init__(self, containers):
        self._containers = containers
        self.list_kwargs = []

    def list(self, all=False, filters=None):
        self.list_kwargs.append({"all": all, "filters": filters})
        return list(self._containers)


class FakeDockerClient:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)


class FakeProber:
    """Canned probe answers, with every port it was asked about."""

    def __init__(self, open_ports=()):
        self.open_ports = set(open_ports)
        self.probed = []

    def __call__(self, port):
        self.probed.append(port)
        return port in self.open_ports


def make_control(containers=(), open_ports=()):
    """A DockerControl on a fake client; returns (control, client, prober)."""
    fake = FakeDockerClient([FakeContainer(name, status) for name, status in containers])
    prober = FakeProber(open_ports)
    control = DockerControl(
        "bitbeard", client_factory=lambda: fake, prober=prober
    )
    return control, fake, prober


def make_client(config, control):
    app = create_app(config=config, docker_control=control)
    return TestClient(app)


# --- Listing (GET /admin/services) ---------------------------------------------


def test_listing_reports_state_probe_and_disable_flag(tmp_path):
    config = make_config(tmp_path)
    control, fake, prober = make_control(
        containers=[("qbittorrent", "running"), ("bot", "exited")],
        open_ports=[8080],
    )

    with make_client(config, control) as client:
        _login(client)
        response = client.get("/admin/services")

    assert response.status_code == 200
    services = {svc["name"]: svc for svc in response.json()["services"]}

    # Only this compose project's Subsystems, in the fixed table order.
    assert list(services) == list(subsystem_ports())

    qb = services["qbittorrent"]
    assert qb["state"] == "running"
    assert qb["probe"] is True
    assert qb["port"] == 8080
    assert qb["optional"] is False
    assert qb["disabled"] is False

    bot = services["bot"]
    assert bot["state"] == "exited"
    assert bot["probe"] is False
    assert bot["optional"] is True

    # The listing was filtered to this compose project's label.
    assert fake.containers.list_kwargs == [
        {"all": True, "filters": {"label": f"{COMPOSE_PROJECT_LABEL}=bitbeard"}}
    ]


def test_listing_covers_every_subsystem_with_defaults(tmp_path):
    config = make_config(tmp_path)
    control, _, _ = make_control()

    with make_client(config, control) as client:
        _login(client)
        services = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }

    # Containers absent from the listing read "absent"; the one-shot
    # init-configs and gluetun have no port to probe. With no Media
    # Server profile enabled there is no media server entry at all.
    assert services["api"]["state"] == "absent"
    assert services["gluetun"]["probe"] is None
    assert services["init-configs"]["probe"] is None
    assert "plex" not in services
    assert "jellyfin" not in services


def test_listing_probes_every_known_port(tmp_path):
    config = make_config(tmp_path)
    control, _, prober = make_control()

    with make_client(config, control) as client:
        _login(client)
        client.get("/admin/services")

    expected = {port for port in subsystem_ports().values() if port is not None}
    assert set(prober.probed) == expected


def test_docker_unavailable_becomes_one_502(tmp_path):
    config = make_config(tmp_path)
    prober = FakeProber()

    def broken_factory():
        raise RuntimeError("no socket")

    control = DockerControl("bitbeard", client_factory=broken_factory, prober=prober)

    with make_client(config, control) as client:
        _login(client)
        listing = client.get("/admin/services")
        action = client.post("/admin/services/api/restart")

    assert listing.status_code == 502
    assert action.status_code == 502
    assert "unavailable" in listing.json()["detail"].lower()


# --- The Media Server slot (COMPOSE_PROFILES) ------------------------------------


def test_plex_profile_lists_plex_on_its_probe_port(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOSE_PROFILES", "plex")
    config = make_config(tmp_path)
    control, _, prober = make_control(open_ports=[32400])

    with make_client(config, control) as client:
        _login(client)
        services = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }

    # One dynamic slot, not two static entries: plex takes it, jellyfin
    # does not appear, and the probe runs against plex's real port.
    assert "plex" in services
    assert "jellyfin" not in services
    assert services["plex"]["port"] == 32400
    assert services["plex"]["probe"] is True
    assert list(services) == list(subsystem_ports())


def test_jellyfin_profile_lists_jellyfin_on_its_probe_port(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOSE_PROFILES", "jellyfin")
    config = make_config(tmp_path)
    control, _, prober = make_control(open_ports=[8096])

    with make_client(config, control) as client:
        _login(client)
        services = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }

    assert "jellyfin" in services
    assert "plex" not in services
    assert services["jellyfin"]["port"] == 8096
    assert services["jellyfin"]["probe"] is True
    assert list(services) == list(subsystem_ports())


def test_no_media_profile_leaves_no_media_server_entry(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    config = make_config(tmp_path)
    control, _, prober = make_control()

    with make_client(config, control) as client:
        _login(client)
        services = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }

    assert "plex" not in services
    assert "jellyfin" not in services
    assert 32400 not in prober.probed
    assert 8096 not in prober.probed


def test_the_media_slot_table_directly(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROFILES", "bot, jellyfin")
    ports = subsystem_ports()
    assert ports["jellyfin"] == 8096
    assert "plex" not in ports

    monkeypatch.setenv("COMPOSE_PROFILES", "plex,bot")
    ports = subsystem_ports()
    assert ports["plex"] == 32400
    assert "jellyfin" not in ports

    monkeypatch.delenv("COMPOSE_PROFILES")
    ports = subsystem_ports()
    assert "plex" not in ports
    assert "jellyfin" not in ports

    # The slot sits where its docker-compose.yml service lives: between
    # the torrent Subsystems and the API.
    monkeypatch.setenv("COMPOSE_PROFILES", "plex")
    assert list(subsystem_ports()) == [
        "gluetun",
        "init-configs",
        "qbittorrent",
        "jackett",
        "plex",
        "api",
        "manager",
        "bot",
        "webui",
    ]


# --- Actions (POST /admin/services/{name}/{action}) -----------------------------


def test_restart_stop_start_reach_the_container(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("api", "running")])

    with make_client(config, control) as client:
        _login(client)
        for action in ("restart", "stop", "start"):
            response = client.post(f"/admin/services/api/{action}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["name"] == "api"
            assert body["action"] == action

    qb = fake.containers._containers[0]
    assert qb.calls == ["restart", "stop", "start"]


def test_stop_and_start_report_the_resulting_state(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("bot", "running")])

    with make_client(config, control) as client:
        _login(client)
        stopped = client.post("/admin/services/bot/stop").json()
        started = client.post("/admin/services/bot/start").json()

    assert stopped["state"] == "exited"
    assert started["state"] == "running"
    assert stopped["disabled"] is False


def test_actions_on_an_absent_container_answer_gracefully(tmp_path):
    config = make_config(tmp_path)
    control, _, _ = make_control()  # nothing exists at all

    with make_client(config, control) as client:
        _login(client)
        response = client.post("/admin/services/gluetun/restart")

    assert response.status_code == 200
    assert response.json()["state"] == "absent"


def test_unknown_subsystem_is_refused_before_any_docker_call(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control()

    with make_client(config, control) as client:
        _login(client)
        for path in ("/admin/services/host/restart", "/admin/services/root/stop"):
            response = client.post(path)
            assert response.status_code == 404

    assert fake.containers.list_kwargs == []
    assert _subsystem_rows(config) == []


def test_unknown_action_is_refused(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("api", "running")])

    with make_client(config, control) as client:
        _login(client)
        response = client.post("/admin/services/api/destroy")

    assert response.status_code == 400
    assert fake.containers._containers[0].calls == []
    assert _subsystem_rows(config) == []


def test_every_action_is_audited_with_actor_and_state(tmp_path):
    config = make_config(tmp_path)
    control, _, _ = make_control(containers=[("jackett", "running")])

    with make_client(config, control) as client:
        _login(client)
        client.post("/admin/services/jackett/restart")
        client.post("/admin/services/jackett/stop")

    rows = _subsystem_rows(config)
    assert [(row["actor"], row["action"]) for row in rows] == [
        ("admin", "subsystem_restart"),
        ("admin", "subsystem_stop"),
    ]
    assert '"subsystem": "jackett"' in rows[0]["details"]
    assert '"state": "running"' in rows[0]["details"]


# --- Disable / enable (the Optional Subsystem only) ------------------------------


def test_optional_subsystem_table_names_only_the_bot():
    assert OPTIONAL_SUBSYSTEMS == {"bot"}


def test_disable_stops_the_bot_and_persists_the_decision(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("bot", "running")])

    with make_client(config, control) as client:
        _login(client)
        response = client.post("/admin/services/bot/disable")

        assert response.status_code == 200
        assert response.json()["disabled"] is True
        assert response.json()["state"] == "exited"

        # The decision survives a reload: even a fresh listing keeps it.
        listing = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }
        assert listing["bot"]["disabled"] is True
        assert listing["bot"]["state"] == "exited"

    assert fake.containers._containers[0].calls == ["stop"]
    rows = _audit_actions(config, "subsystem_disabled")
    assert len(rows) == 1
    assert rows[0]["actor"] == "admin"


def test_disable_persists_when_the_container_is_gone(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("bot", "running")])

    with make_client(config, control) as client:
        _login(client)
        client.post("/admin/services/bot/disable")

    # Simulate the container vanishing (compose down / removal): the
    # disable flag is the Admin's decision, not a container state.
    fake.containers._containers.clear()

    with make_client(config, control) as client:
        _login(client)
        listing = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }

    assert listing["bot"]["disabled"] is True
    assert listing["bot"]["state"] == "absent"


def test_disable_works_when_the_bot_is_already_stopped(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("bot", "exited")])

    with make_client(config, control) as client:
        _login(client)
        response = client.post("/admin/services/bot/disable")

    assert response.status_code == 200
    assert response.json()["disabled"] is True
    assert response.json()["state"] == "exited"


def test_enable_clears_the_flag_and_starts_the_bot(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("bot", "running")])

    with make_client(config, control) as client:
        _login(client)
        client.post("/admin/services/bot/disable")
        response = client.post("/admin/services/bot/enable")

        assert response.status_code == 200
        assert response.json()["disabled"] is False
        assert response.json()["state"] == "running"

        listing = {
            svc["name"]: svc for svc in client.get("/admin/services").json()["services"]
        }
        assert listing["bot"]["disabled"] is False

    bot = fake.containers._containers[0]
    assert bot.calls == ["stop", "start"]
    rows = _audit_actions(config, "subsystem_enabled")
    assert len(rows) == 1


def test_enable_on_an_absent_container_still_clears_the_flag(tmp_path):
    config = make_config(tmp_path)
    control, _, _ = make_control()

    with make_client(config, control) as client:
        _login(client)
        response = client.post("/admin/services/bot/enable")

    assert response.status_code == 200
    assert response.json()["disabled"] is False
    assert response.json()["state"] == "absent"


def test_disable_and_enable_are_refused_for_mandatory_subsystems(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("webui", "running")])

    with make_client(config, control) as client:
        _login(client)
        for name in ("webui", "gluetun", "jackett", "api", "manager"):
            disable = client.post(f"/admin/services/{name}/disable")
            enable = client.post(f"/admin/services/{name}/enable")
            assert disable.status_code == 400, name
            assert enable.status_code == 400, name
            assert "Optional Subsystem" in disable.json()["detail"]

    assert fake.containers._containers[0].calls == []
    assert _subsystem_rows(config) == []
    assert not db.service_disabled(config.db_path, "webui")


# --- Role gating -----------------------------------------------------------------


def test_subsystem_endpoints_refuse_the_user_role(tmp_path):
    config = make_config(tmp_path)
    control, fake, _ = make_control(containers=[("api", "running")])

    with make_client(config, control) as client:
        db.create_account(
            config.db_path, username="deckhand", password="ropes123", role="user"
        )
        _login(client, "deckhand", "ropes123")

        assert client.get("/admin/services").status_code == 403
        assert client.post("/admin/services/api/restart").status_code == 403
        assert client.post("/admin/services/bot/disable").status_code == 403

    assert fake.containers._containers[0].calls == []
    assert _subsystem_rows(config) == []


def test_subsystem_endpoints_require_a_session(tmp_path):
    config = make_config(tmp_path)
    control, _, _ = make_control()

    with make_client(config, control) as client:
        assert client.get("/admin/services").status_code == 401
        assert client.post("/admin/services/api/restart").status_code == 401


# --- Disable state storage --------------------------------------------------------


def test_service_disabled_defaults_to_false_for_unknown_names(tmp_path):
    config = make_config(tmp_path)
    db.initialize(config.db_path)

    assert db.service_disabled(config.db_path, "bot") is False


def test_set_service_disabled_round_trips(tmp_path):
    config = make_config(tmp_path)
    db.initialize(config.db_path)

    db.set_service_disabled(config.db_path, "bot", True)
    assert db.service_disabled(config.db_path, "bot") is True
    db.set_service_disabled(config.db_path, "bot", False)
    assert db.service_disabled(config.db_path, "bot") is False


# --- Proxy seams untouched --------------------------------------------------------


def test_app_wires_the_injected_control_alongside_the_manager_proxy(tmp_path):
    config = make_config(tmp_path)
    control, _, _ = make_control()

    class FakeTransport:
        def __call__(self, method, url, *, headers, params=None, json=None):
            return ProxyResponse(200, {"status": "ok"})

    app = create_app(
        config=config, proxy_transport=FakeTransport(), docker_control=control
    )

    assert app.state.docker_control is control
    assert app.state.manager_proxy is not None
