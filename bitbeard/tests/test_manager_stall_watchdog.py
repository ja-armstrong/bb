"""Tests for the Stall Watchdog (manager.stall_watchdog) and its wiring.

Each group maps to the escalation ladder: a stalled torrent gets a forced
reannounce on its first sighting, the listening port rotates (the wedged
UDP socket cure) on every ``port_rotate_polls``-th consecutive sighting,
and anything that starts moving again is dropped from tracking.
"""

from fastapi.testclient import TestClient

from manager.config import Config
from manager.main import create_app
from manager.stall_watchdog import StallWatchdog


class FakeTorrent:
    def __init__(
        self,
        hash,
        name,
        state="stalledDL",
        downloaded=0,
        dlspeed=0,
        num_leechs=0,
    ):
        self.hash = hash
        self.name = name
        self.state = state
        self.downloaded = downloaded
        self.dlspeed = dlspeed
        self.num_leechs = num_leechs


class FakeQbt:
    """Scriptable stand-in: records reannounces and preference writes."""

    def __init__(self, torrents=(), listen_port=6789):
        self.torrents = list(torrents)
        self.listen_port = listen_port
        self.reannounced_hashes = []
        self.reannounced_all = False
        self.set_preferences_calls = []

    def torrents_info(self, status_filter=None):
        return list(self.torrents)

    def torrents_reannounce(self, torrent_hashes=None):
        if torrent_hashes is None:
            self.reannounced_all = True
        else:
            self.reannounced_hashes.append(torrent_hashes)

    def app_preferences(self):
        return {"listen_port": self.listen_port}

    def app_set_preferences(self, prefs):
        self.set_preferences_calls.append(dict(prefs))
        self.listen_port = prefs["listen_port"]


def make_watchdog(qbt, port_rotate_polls=3):
    return StallWatchdog(qbt, port_rotate_polls=port_rotate_polls)


def stalled(hash, name="Stuck Torrent", state="metaDL"):
    return FakeTorrent(hash, name, state=state)


def healthy(hash, name="Moving Torrent"):
    return FakeTorrent(
        hash, name, state="downloading", downloaded=1024, dlspeed=1_000_000,
        num_leechs=3,
    )


def test_first_stalled_sighting_reannounces_only():
    qbt = FakeQbt([stalled("a" * 40)])
    watchdog = make_watchdog(qbt)

    actions = watchdog.check_once()

    assert actions == {"a" * 40: "forced reannounce"}
    assert qbt.reannounced_hashes == ["a" * 40]
    assert qbt.set_preferences_calls == []  # the port stays put
    assert qbt.reannounced_all is False


def test_healthy_torrent_is_never_touched():
    qbt = FakeQbt([healthy("b" * 40)])
    watchdog = make_watchdog(qbt)

    assert watchdog.check_once() == {}
    assert qbt.reannounced_hashes == []
    assert qbt.set_preferences_calls == []


def test_quiet_downloading_torrent_with_peers_is_not_stalled():
    # A healthy-but-quiet moment: state "downloading", peers present,
    # zero speed this instant. Not the watchdog's business.
    quiet = FakeTorrent(
        "c" * 40, "Quiet Torrent", state="downloading", downloaded=0,
        dlspeed=0, num_leechs=2,
    )
    qbt = FakeQbt([quiet])
    watchdog = make_watchdog(qbt)

    assert watchdog.check_once() == {}
    assert qbt.reannounced_hashes == []


def test_persistent_stall_rotates_port_and_reannounces_all():
    torrent = stalled("d" * 40)
    qbt = FakeQbt([torrent])
    watchdog = make_watchdog(qbt, port_rotate_polls=3)

    # Sweeps 1 and 2 stay on the gentle rung: only the first sighting
    # reannounces, sweep 2 sits inside the escalation window.
    watchdog.check_once()
    watchdog.check_once()
    assert qbt.set_preferences_calls == []
    assert qbt.reannounced_hashes == ["d" * 40]

    # Sweep 3: the socket is presumed wedged, so the port rotates and
    # every torrent reannounces under it.
    actions = watchdog.check_once()
    assert qbt.set_preferences_calls == [
        {"listen_port": qbt.listen_port}
    ]
    assert 20000 <= qbt.listen_port <= 60000
    assert qbt.listen_port != 6789  # never the port it just came from
    assert qbt.reannounced_all is True
    assert "rotated listening port to" in actions["d" * 40]


def test_port_rotation_repeats_only_each_rotation_window():
    torrent = stalled("e" * 40)
    qbt = FakeQbt([torrent])
    watchdog = make_watchdog(qbt, port_rotate_polls=3)

    for _ in range(6):
        watchdog.check_once()

    # Rotations at sweeps 3 and 6, reannounces all along.
    assert len(qbt.set_preferences_calls) == 2
    assert qbt.reannounced_all is True


def test_recovering_torrent_is_forgotten_and_starts_over():
    torrent = stalled("f" * 40)
    qbt = FakeQbt([torrent])
    watchdog = make_watchdog(qbt, port_rotate_polls=3)

    watchdog.check_once()
    assert watchdog._streaks == {"f" * 40: 1}

    # The reannounce (or the world) un-wedged it: it moves again.
    torrent.state = "downloading"
    torrent.downloaded = 1024
    torrent.dlspeed = 500_000
    torrent.num_leechs = 1
    watchdog.check_once()
    assert watchdog._streaks == {}

    # And a later stall starts over at the gentle rung.
    torrent.state = "stalledDL"
    torrent.downloaded = 0
    torrent.dlspeed = 0
    torrent.num_leechs = 0
    watchdog.check_once()
    assert watchdog._streaks == {"f" * 40: 1}
    assert qbt.set_preferences_calls == []


def test_rotated_port_never_collides_with_the_current_one():
    torrent = stalled("a" * 40)
    qbt = FakeQbt([torrent])  # current port 6789
    # A range where every value but one collides with the current port:
    # the rotation must still land on the one safe port.
    watchdog = StallWatchdog(
        qbt, port_rotate_polls=2, port_range=range(6789, 6791)
    )
    for _ in range(2):
        watchdog.check_once()

    assert qbt.listen_port == 6790


def test_watchdog_wiring_follows_the_interval(tmp_path):
    def make(interval, **kwargs):
        config = Config(
            qbit_username="admin",
            qbit_password="hunter2",
            base_download_dir=str(tmp_path / "media"),
            jackett_api_key="key",
            service_token="token",
            db_path=str(tmp_path / "bitbeard.db"),
            reconcile_interval=0.0,
            stall_watch_interval=interval,
            **kwargs,
        )
        return create_app(config=config, qbt_client=FakeQbt(), announcer=None)

    # On by default: the lifespan starts the sweeper.
    application = make(300.0)
    with TestClient(application):
        assert isinstance(application.state.stall_watchdog, StallWatchdog)
        assert application.state.stall_watchdog_task is not None

    # Zero disables the job, like the Quota reconcile's interval.
    application = make(0.0)
    with TestClient(application):
        assert not hasattr(application.state, "stall_watchdog")
