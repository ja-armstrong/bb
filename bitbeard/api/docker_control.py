"""Docker control for the Admin Panel's Subsystems page (ADR-0003).

The API is the only service with the docker socket mounted, and this module
is the one place the socket is touched from: listing this compose project's
containers and running restart/stop/start on a known Subsystem. The socket
is root-equivalent on the host, so the mitigations live here in code, not in
convention:

- the listing is filtered to containers carrying this compose project's
  ``com.docker.compose.project`` label;
- only the fixed Subsystem names from ``docker-compose.yml`` are
  controllable — anything else raises ``UnknownSubsystem`` before a single
  docker call happens;
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
import socket
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: The label docker-compose stamps on every container it creates. Filtering
#: on it keeps the socket's root-equivalent power scoped to this stack.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

#: The known Subsystems (the container names from ``docker-compose.yml``)
#: and the port a reachability probe tries. Everything but plex shares
#: gluetun's network namespace, so ``127.0.0.1`` is the right probe host from
#: the API's side; plex is listed for parity with the panel. ``None`` means
#: no port of its own to probe (gluetun only forwards others'; init-configs
#: is one-shot and listens for nothing).
SUBSYSTEM_PORTS: dict[str, int | None] = {
    "gluetun": None,
    "init-configs": None,
    "qbittorrent": 8080,
    "jackett": 9117,
    "plex": 32400,
    "api": 9870,
    "manager": 9871,
    "bot": 9872,
    "webui": 80,
}

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
            for name in SUBSYSTEM_PORTS
        }

    def probe(self, name: str) -> bool | None:
        """Reachability of a Subsystem's known port, or ``None`` for the
        Subsystems that listen on no port of their own."""
        port = SUBSYSTEM_PORTS[name]
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
        if name not in SUBSYSTEM_PORTS:
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
