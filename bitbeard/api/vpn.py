"""VPN settings and the egress probe for the Admin Panel's VPN page.

The API owns the Admin Panel's VPN capabilities (it owns docker control and
the Audit Ledger), and the smallest workable mechanism for editing the VPN
is a settings file next to the OpenVPN config the gluetun Subsystem mounts:

- ``openvpn/vpn.env`` — a ``.env``-style ``KEY=value`` file holding the
  provider and the OpenVPN credentials the panel wrote (``VPN_PROVIDER``,
  ``OPENVPN_USER``, ``OPENVPN_PASSWORD``, ``OPENVPN_SERVER``). It is the
  store of record for panel edits; the compose ``.env`` seeds the first
  provider via the ``VPN_PROVIDER`` fallback.
- ``openvpn/custom.ovpn`` — gluetun's OpenVPN config. When the panel sets a
  server, the ``remote`` and ``verify-x509-name`` lines are rewritten so a
  **container restart** picks the new server up (gluetun re-launches
  OpenVPN — and re-reads the config file — on restart; the container's
  environment is fixed at creation, so provider/credential changes are
  applied on the next ``docker compose up`` after a host-side ``.env``
  update. The panel restarts gluetun after every save either way).

Every container in the stack rides gluetun's network namespace
(``network_mode: service:gluetun``), so the API's own egress *is* the VPN
egress: the probe asks a public IP-echo service from inside the API and
reports the answer as the current exit IP.

``egress_transport`` is the seam for tests — a callable from the echo URL
to the public IP text (``None`` when the probe fails). The default imports
``requests`` lazily, so importing ``api.main`` stays dependency-free, the
same rule ``manager_proxy`` keeps.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

#: The public IP-echo service the netns-sharing probe asks.
EGRESS_URL = "https://api.ipify.org/?format=json"

#: The gluetun-mounted OpenVPN config and the panel's settings file.
DEFAULT_CONFIG_FILENAME = "custom.ovpn"
DEFAULT_SETTINGS_FILENAME = "vpn.env"

#: The keys the settings file holds, in the order it writes them.
SETTINGS_KEYS = (
    "VPN_PROVIDER",
    "OPENVPN_USER",
    "OPENVPN_PASSWORD",
    "OPENVPN_SERVER",
)

#: The transport: one HTTP round-trip to the echo URL, answering with the
#: public IP as text — or ``None`` when the probe failed (a probe that
#: raises is a broken probe; reporting "unknown" is the honest answer).
EgressTransport = Callable[[str], str | None]


@dataclass(frozen=True)
class VpnSettings:
    """The VPN configuration as the panel sees it.

    ``provider`` falls back to the compose ``.env`` value until the panel
    writes its own; ``server`` comes from the settings file when set, or
    from the OpenVPN config's ``remote`` line when not.
    """

    provider: str | None
    openvpn_user: str | None
    openvpn_password: str | None
    server: str | None


def _default_egress_transport(url: str) -> str | None:
    """One blocking probe to the IP-echo service, failures read as unknown."""
    import requests  # lazy: keeps `import api.main` dependency-free

    try:
        response = requests.get(url, timeout=10.0)
        payload = response.json()
        return payload.get("ip")
    except Exception as exc:
        logger.warning("Egress probe to %s failed: %s", url, exc)
        return None


class VpnControl:
    """Read/write the VPN settings and probe the egress IP.

    Deep module: callers name settings and get settings back; the file
    format, the OpenVPN config rewrite and the probe plumbing stay inside.
    ``openvpn_dir`` is the directory the compose file mounts the host's
    ``./openvpn`` into; ``provider_fallback`` is the ``VPN_PROVIDER`` the
    compose ``.env`` seeded.
    """

    def __init__(
        self,
        openvpn_dir: str,
        *,
        provider_fallback: str = "",
        config_filename: str = DEFAULT_CONFIG_FILENAME,
        settings_filename: str = DEFAULT_SETTINGS_FILENAME,
        egress_transport: EgressTransport | None = None,
    ):
        self._openvpn_dir = openvpn_dir
        self._provider_fallback = provider_fallback
        self._config_path = os.path.join(openvpn_dir, config_filename)
        self._settings_path = os.path.join(openvpn_dir, settings_filename)
        self._egress_transport = egress_transport or _default_egress_transport

    # --- Reading -------------------------------------------------------------

    def read_settings(self) -> VpnSettings:
        """The current settings: file values, fallbacks applied."""
        stored = self._read_env_file(self._settings_path)
        provider = stored.get("VPN_PROVIDER") or self._provider_fallback
        server = stored.get("OPENVPN_SERVER") or self._config_server()
        return VpnSettings(
            provider=provider or None,
            openvpn_user=stored.get("OPENVPN_USER"),
            openvpn_password=stored.get("OPENVPN_PASSWORD"),
            server=server,
        )

    # --- Writing -------------------------------------------------------------

    def update_settings(
        self,
        *,
        provider: str | None = None,
        openvpn_user: str | None = None,
        openvpn_password: str | None = None,
        server: str | None = None,
    ) -> VpnSettings:
        """Merge the given settings into the file; rewrite the config's
        server lines when the server changes.

        Fields left as ``None`` are untouched, so a panel edit that only
        changes the provider does not clobber the credentials.
        """
        changes = {
            "VPN_PROVIDER": provider,
            "OPENVPN_USER": openvpn_user,
            "OPENVPN_PASSWORD": openvpn_password,
            "OPENVPN_SERVER": server,
        }
        stored = self._read_env_file(self._settings_path)
        for key, value in changes.items():
            if value is not None:
                stored[key] = value.strip()
        self._write_env_file(stored)
        if server is not None:
            self._rewrite_config_server(server.strip())
        return self.read_settings()

    # --- Probing --------------------------------------------------------------

    def egress_ip(self) -> str | None:
        """The current public exit IP, or ``None`` when the probe failed.

        Runs in the API's own network namespace — gluetun's, shared by
        every Subsystem — so the answer is the VPN egress the whole stack
        actually exits through.
        """
        try:
            return self._egress_transport(EGRESS_URL)
        except Exception as exc:
            logger.warning("Egress probe failed: %s", exc)
            return None

    # --- File plumbing ---------------------------------------------------------

    def _read_env_file(self, path: str) -> dict[str, str]:
        """Parse a ``.env``-style ``KEY=value`` file (comments ignored)."""
        if not os.path.isfile(path):
            return {}
        stored: dict[str, str] = {}
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                stored[key.strip()] = value.strip()
        return stored

    def _write_env_file(self, stored: dict[str, str]) -> None:
        """Write the settings file with the known keys, in a fixed order."""
        os.makedirs(self._openvpn_dir, exist_ok=True)
        with open(self._settings_path, "w", encoding="utf-8") as handle:
            for key in SETTINGS_KEYS:
                if key in stored:
                    handle.write(f"{key}={stored[key]}\n")

    def _config_server(self) -> str | None:
        """The server the OpenVPN config currently names, if readable."""
        try:
            with open(self._config_path, encoding="utf-8") as handle:
                for line in handle:
                    match = re.match(r"remote\s+(\S+)", line.strip())
                    if match:
                        return match.group(1)
        except OSError:
            return None
        return None

    def _rewrite_config_server(self, server: str) -> None:
        """Rewrite the config's ``remote``/``verify-x509-name`` lines.

        The port rides along from the existing line (1194 in the shipped
        template). With no config file to rewrite, the settings file alone
        still carries the server for the status view — the config is
        (re)created out of band.
        """
        try:
            with open(self._config_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            logger.warning(
                "OpenVPN config %s unreadable; server stored in settings "
                "only (%s).",
                self._config_path, exc,
            )
            return

        rewritten, remote_lines = re.subn(
            r"(?m)^remote\s+\S+(\s+\d+)?\s*$",
            lambda match: f"remote {server}{match.group(1) or ''}",
            text,
        )
        rewritten = re.sub(
            r"(?m)^verify-x509-name\s+.*$",
            f"verify-x509-name CN={server}",
            rewritten,
        )
        if remote_lines == 0:
            # No remote line to rewrite: append one at the default port so
            # the change is still applied (OpenVPN reads it wherever it
            # sits in the file).
            rewritten = text.rstrip("\n") + f"\nremote {server} 1194\n"
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(rewritten)
