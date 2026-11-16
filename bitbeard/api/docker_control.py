"""Docker control for the Admin Panel's Subsystems page (ADR-0003).

The API is the only service with the docker socket mounted, and this module
is the one place the socket is touched from: listing this compose project's
containers and running restart/stop/start on a known Subsystem. The socket
is root-equivalent on the host, so the mitigations live here in code, not in
convention:

- the listing is filtered to containers carrying this compose project's
  ``com.docker.compose.project`` label;
- only the known Subsystem names are controllable — the mandatory ones
  from ``docker-compose.yml`` plus the Media Server slot derived from
  ``COMPOSE_PROFILES`` — anything else raises ``UnknownSubsystem`` before
  a single docker call happens;
- the caller (``api.main``) gates every action behind the Admin Role and
  writes an Audit Ledger row for it.

``client_factory`` is the seam for tests: a zero-argument callable returning
a docker client-shaped object (``client.containers.list(all=..., filters=...)``
plus container ``.name``/``.status`` and ``.restart()/.stop()/.start()``).
The default factory imports the docker SDK lazily, so importing this module
— and running the whole test suite — needs nothing but the stdlib, the same
import-safety rule ``manager_proxy`` keeps for ``requests``.
"""

import logging
import os
import re
import socket
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: The label docker-compose stamps on every container it creates. Filtering
#: on it keeps the socket's root-equivalent power scoped to this stack.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

#: The mandatory Subsystems (the container names from ``docker-compose.yml``)
#: and the port a reachability probe tries. Everything but the Media Server
#: shares gluetun's network namespace, so ``127.0.0.1`` is the right probe
#: host from the API's side; the Media Server is listed for parity with the
#: panel. ``None`` means no port of its own to probe (gluetun only forwards
#: others'; init-configs is one-shot and listens for nothing).
MANDATORY_SUBSYSTEM_PORTS: dict[str, int | None] = {
    "gluetun": None,
    "init-configs": None,
    "qbittorrent": 8080,
    "jackett": 9117,
    "api": 9870,
    "manager": 9871,
    "bot": 9872,
    "webui": 80,
}

#: The Media Server's probe port, whichever flavor the environment enables.
MEDIA_SERVER_PORTS: dict[str, int] = {
    "jellyfin": 8096,
    "plex": 32400,
}


def media_server() -> str | None:
    """The Media Server ``COMPOSE_PROFILES`` enables, or ``None``.

    The compose profiles are the source of truth for which Media Server
    the stack boots, so the panel's one dynamic slot derives from the
    same variable the ``api`` service is handed — the panel can never
    disagree with what is actually running. Only one flavor belongs in a
    stack (the Media Server is chosen once at Bootstrap); if both are
    listed, jellyfin wins, the same order the guidance names them in.
    The value is split on commas and whitespace alike: the wizard writes
    it space-separated, hand-edits and the panel comma-separated.
    """
    profiles = {
        name.strip()
        for name in re.split(r"[,\s]+", os.environ.get("COMPOSE_PROFILES", ""))
        if name.strip()
    }
    for candidate in MEDIA_SERVER_PORTS:
        if candidate in profiles:
            return candidate
    return None


def subsystem_ports() -> dict[str, int | None]:
    """The known Subsystems and their probe ports, with the Media Server
    slot filled from ``COMPOSE_PROFILES``.

    The Media Server entry sits between the torrent Subsystems and the
    API — where its ``docker-compose.yml`` service lives — and vanishes
    entirely when no flavor is enabled. Read per call, so a ``docker
    compose up`` with new profiles is all a change takes.
    """
    ports: dict[str, int | None] = {}
    media = media_server()
    for name, port in MANDATORY_SUBSYSTEM_PORTS.items():
        if name == "api" and media is not None:
            ports[media] = MEDIA_SERVER_PORTS[media]
        ports[name] = port
    return ports

#: Subsystems that may be disabled from the Admin Panel. Every other
#: Subsystem is mandatory and restartable only.
OPTIONAL_SUBSYSTEMS = frozenset({"bot"})

#: The container actions a Subsystem supports, and the state the container
#: is in after each succeeds (asked fresh of docker, these are the same
#: answer — reading a possibly-stale ``container.status`` would not be).
CONTAINER_ACTIONS = ("restart", "stop", "start")
ACTION_STATE = {"restart": "running", "stop": "exited", "start": "running"}

#: How long a reachability probe waits before declaring the port closed.
PROBE_TIMEOUT = 1.0


class UnknownSubsystem(Exception):
    """A name outside the fixed Subsystem table was asked for by action."""


class DockerUnavailable(Exception):
    """The docker socket could not be reached or answered unusably."""


def probe_tcp(port: int, *, host: str = "127.0.0.1") -> bool:
    """One short TCP connect; True when something is listening.

    Runs in the API's own network namespace — gluetun's, shared by every
    Subsystem that rides it — so a listening port answers on localhost.
    Any refusal, timeout or unreachable host is simply "not reachable".
    """
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError as exc:
        logger.debug("Probe to %s:%s failed: %s", host, port, exc)
        return False


def _default_client_factory() -> Callable[[], Any]:
    """The real docker client, built on first use.

    The SDK import is lazy: ``api.main`` (and the import-safety test) must
    stay free of it, and a client object itself connects only on first call.
    """
    def factory():
        import docker  # lazy: keeps `import api.main` dependency-free

        return docker.from_env()

    return factory


class DockerControl:
    """The API's one seam to the docker socket (ADR-0003).

    Deep module: callers name a Subsystem and an action and get a state
    back; label filtering, the Subsystem name table and the container
    plumbing stay inside. ``client_factory`` (tests inject a fake) and
    ``prober`` (tests inject canned answers) are the seams.
    """

    def __init__(
        self,
        compose_project: str,
        client_factory: Callable[[], Any] | None = None,
        prober: Callable[[int], bool] | None = None,
    ):
        self.compose_project = compose_project
        self._client_factory = client_factory or _default_client_factory()
        self._prober = prober or probe_tcp

    # --- reading -------------------------------------------------------------

    def statuses(self) -> dict[str, str]:
        """Every known Subsystem's container state.

        A Subsystem whose container is gone from the listing (it was never
        created, or ``docker compose down`` removed it) reads ``absent``.
        The listing is label-filtered and ``all``-inclusive, so a stopped —
        but not removed — Subsystem still shows its real state.
        """
        containers = self._containers()
        return {
            name: containers[name].status if name in containers else "absent"
            for name in subsystem_ports()
        }

    def probe(self, name: str) -> bool | None:
        """Reachability of a Subsystem's known port, or ``None`` for the
        Subsystems that listen on no port of their own."""
        port = subsystem_ports()[name]
        if port is None:
            return None
        return self._prober(port)

    # --- acting --------------------------------------------------------------

    def act(self, name: str, action: str) -> str:
        """Run one container action on a Subsystem; returns its state after.

        An absent container is handled gracefully — there is nothing to
        act on, so the state comes back ``absent`` without an error (this
        is how a disabled Bot, or a stack torn down on the host, behaves).
        Unknown names and actions are refused before any docker call.
        """
        if name not in subsystem_ports():
            raise UnknownSubsystem(name)
        if action not in CONTAINER_ACTIONS:
            raise ValueError(f"Unknown container action: {action}")

        container = self._containers().get(name)
        if container is None:
            return "absent"
        try:
            getattr(container, action)()
        except DockerUnavailable:
            raise
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc
        return ACTION_STATE[action]

    # --- plumbing ------------------------------------------------------------

    def _containers(self) -> dict[str, Any]:
        """This compose project's containers, keyed by container name."""
        client = self._client()
        # docker-py's label filter shape: {"label": "<key>=<value>"}.
        label_filter = {
            "label": f"{COMPOSE_PROJECT_LABEL}={self.compose_project}"
        }
        try:
            containers = client.containers.list(all=True, filters=label_filter)
        except DockerUnavailable:
            raise
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc
        return {container.name: container for container in containers}

    def _client(self):
        try:
            return self._client_factory()
        except DockerUnavailable:
            raise
        except Exception as exc:
            raise DockerUnavailable(str(exc)) from exc
