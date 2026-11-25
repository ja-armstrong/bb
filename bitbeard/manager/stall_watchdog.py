"""Stall watchdog: automated recovery for wedged torrent UDP sockets.

libtorrent routes all of a torrent's UDP traffic - DHT, PeX and every
tracker announce - through one socket, bound once when the listening
port is set. When that bind happens while the VPN tunnel is down (gluetun
retries OpenVPN for as long as it takes, and qBittorrent keeps running),
every later send() fails with "Operation not permitted" forever: all
trackers report it, DHT stays empty, and the download sits at 0 bytes
however long the tunnel stays up afterwards. The manual cure is the
"random port + force reannounce" ritual: changing the listening port
forces the rebind, and a reannounce starts fresh announces over it.

The watchdog automates that cure. On an interval it looks for torrents
in the download phase that cannot move a byte (no leeches, no speed,
nothing downloaded) and escalates: a first sighting gets a forced
reannounce (the gentle fix for a missed or throttled announce), a
torrent still stalled ``port_rotate_polls`` sweeps later gets the
listening port rotated to a fresh random value first - which rebinds the
shared socket - followed by a reannounce of every torrent under the new
port. Torrents that start moving are dropped from tracking, so a
healthy-but-quiet transfer is never touched, and trackers that are dead
on their own (DNS failures and the like) keep their qBittorrent state -
the watchdog only ever reannounces, it does not decide tracker health.

All qBittorrent access happens through the injected client, never
created here. The sweep interval comes from the Config; ``0`` disables
the job, like the Storage Quota reconcile's.
"""

import logging
import random

logger = logging.getLogger(__name__)

#: qBittorrent states meaning "in the download phase, but finding
#: nobody": ``metaDL`` (still fetching metadata - the state a wedged
#: torrent sits in for hours), ``forcedMetaDL`` and ``stalledDL`` (lost
#: all peers). A healthy-but-quiet moment of plain ``downloading`` is
#: excluded by the state list, not just by the zero-traffic test below.
STALLED_STATES = frozenset({"stalledDL", "metaDL", "forcedMetaDL"})

#: The rotation range stays far above every port the shared netns
#: already uses for published or internal services (WebUI 8080, Jackett
#: 9117, gluetun DNS 53, API/manager/bot 9870-9872, Jellyfin 8096, the
#: SSL listener), so a rotated port can never collide with one.
PORT_RANGE = range(20000, 60001)

DEFAULT_STALL_WATCH_INTERVAL_SECONDS = 300.0
DEFAULT_STALL_PORT_ROTATE_POLLS = 3


class StallWatchdog:
    """Detects un-moving downloads and applies the escalation ladder."""

    def __init__(
        self,
        qbt_client,
        port_rotate_polls: int = DEFAULT_STALL_PORT_ROTATE_POLLS,
        port_range=PORT_RANGE,
    ):
        self._qbt = qbt_client
        self._port_rotate_polls = max(2, port_rotate_polls)
        self._port_range = port_range
        self._streaks: dict[str, int] = {}  # hash -> consecutive stalled sweeps
        self.last_actions: dict[str, str] = {}  # hash -> last action taken

    def stalled_torrents(self) -> list:
        """Torrents in the download phase that cannot move a byte."""
        stalled = []
        for torrent in self._qbt.torrents_info():
            state = getattr(torrent, "state", None)
            if state not in STALLED_STATES:
                continue
            if (
                getattr(torrent, "downloaded", 0) == 0
                and getattr(torrent, "dlspeed", 0) == 0
                and getattr(torrent, "num_leechs", 0) == 0
            ):
                stalled.append(torrent)
        return stalled

    def check_once(self) -> dict:
        """One sweep: reannounce, escalate to a port rotation, forget.

        Returns a mapping of torrent hash to the action taken this sweep
        (empty when nothing is stalled). The escalation ladder: the first
        stalled sighting of a torrent gets a forced reannounce; every
        ``port_rotate_polls``-th consecutive sighting rotates the
        listening port first, so a wedged UDP socket is rebinded instead
        of reannouncing through it a third time in vain.
        """
        stalled = self.stalled_torrents()
        stalled_hashes = {torrent.hash for torrent in stalled}
        actions: dict[str, str] = {}

        for torrent in stalled:
            streak = self._streaks.get(torrent.hash, 0) + 1
            self._streaks[torrent.hash] = streak
            if streak == 1:
                self._reannounce_torrent(torrent.hash)
                action = "forced reannounce"
            elif streak % self._port_rotate_polls == 0:
                port = self._rotate_port()
                # The rotation rebinds the shared UDP socket for every
                # torrent, so all of them reannounce under the new port.
                self._qbt.torrents_reannounce()
                action = f"rotated listening port to {port} and reannounced all"
            else:
                continue  # inside an escalation window; next sweep decides
            self.last_actions[torrent.hash] = action
            actions[torrent.hash] = action
            logger.warning(
                "Stall watchdog: '%s' (%s...) stalled at 0 bytes for %d "
                "sweeps -> %s.",
                torrent.name,
                torrent.hash[:8],
                streak,
                action,
            )

        # A torrent that moved again (or finished, or was removed) leaves
        # the ladder; its next stall starts over at a gentle reannounce.
        for hash_ in list(self._streaks):
            if hash_ not in stalled_hashes:
                del self._streaks[hash_]
        return actions

    def _reannounce_torrent(self, torrent_hash: str) -> None:
        self._qbt.torrents_reannounce(torrent_hashes=torrent_hash)

    def _rotate_port(self) -> int:
        """Point qBittorrent at a fresh listening port and return it."""
        current = self._qbt.app_preferences().get("listen_port")
        port = random.choice(self._port_range)
        while port == current:
            port = random.choice(self._port_range)
        self._qbt.app_set_preferences({"listen_port": port})
        return port
