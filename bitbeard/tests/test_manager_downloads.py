"""Tests for the deep Download module (manager.downloads)."""
import asyncio
import os

from manager.config import Config
from manager.downloads import DownloadManager, format_eta


class FakeTorrent:
    def __init__(self, hash, name, progress=0.5, eta=3600, status="downloading",
                 polls_until_complete=None):
        self.hash = hash
        self.name = name
        self.progress = progress
        self.eta = eta
        self.status = status
        self.polls_until_complete = polls_until_complete
        self.save_path = None


class FakeQbt:
    """Scriptable stand-in for the qbittorrentapi client."""

    def __init__(self):
        self.torrents = []
        self.add_calls = []
        self.delete_calls = []
        self.add_error = None
        self._next_hash = 0

    def add_torrent(self, hash, name, **kwargs):
        self.torrents.append(FakeTorrent(hash, name, **kwargs))

    def torrents_add(self, urls=None, save_path=None):
        self.add_calls.append({"urls": urls, "save_path": save_path})
        if self.add_error is not None:
            raise self.add_error
        # A torrent appears in the client after a successful add.
        self._next_hash += 1
        self.torrents.append(
            FakeTorrent(f"newhash{self._next_hash}", "Test Torrent",
                        polls_until_complete=2)
        )
        return "Ok."

    def torrents_info(self, status_filter=None):
        for torrent in self.torrents:
            if (torrent.polls_until_complete is not None
                    and status_filter == "downloading"):
                torrent.polls_until_complete -= 1
                if torrent.polls_until_complete <= 0:
                    torrent.status = "completed"
                    torrent.polls_until_complete = None
        if status_filter is None:
            return list(self.torrents)
        return [t for t in self.torrents if t.status == status_filter]

    def torrents_delete(self, torrent_hashes=None):
        self.delete_calls.append(torrent_hashes)
        self.torrents = [t for t in self.torrents if t.hash != torrent_hashes]


class CollectingAnnouncer:
    def __init__(self):
        self.submitted = []
        self.completed = []
        self.all_done_count = 0

    async def announce_submitted(self, torrent_name, category, actor=None,
                                 download_id=None):
        self.submitted.append((torrent_name, category, actor, download_id))

    async def announce_completed(self, torrent_name, category, actor=None,
                                 download_id=None):
        self.completed.append((torrent_name, category, actor, download_id))

    async def announce_all_done(self):
        self.all_done_count += 1


def make_config():
    return Config(
        qbit_username="admin",
        qbit_password="hunter2",
        base_download_dir="/media/downloads",
        jackett_api_key="jackett-key",
        service_token="service-token",
    )


def make_manager(qbt=None, announcer=None, poll_interval=0.01):
    qbt = qbt if qbt is not None else FakeQbt()
    announcer = announcer if announcer is not None else CollectingAnnouncer()
    manager = DownloadManager(
        qbt, make_config(), announcer, poll_interval=poll_interval
    )
    return manager, qbt, announcer


def test_format_eta_formats_hours_minutes_seconds():
    assert format_eta(0) == "0h:0m:0s"
    assert format_eta(125) == "0h:2m:5s"
    assert format_eta(3723) == "1h:2m:3s"


def test_progress_shapes_dicts_exactly_like_the_api_contract():
    qbt = FakeQbt()
    qbt.add_torrent("h1", "Some Show", progress=0.1234, eta=3723,
                    status="downloading")
    manager, _, _ = make_manager(qbt=qbt)

    rows = manager.progress()

    assert rows == [
        {"name": "Some Show", "progress": "12.34%", "eta": "1h:2m:3s"}
    ]


def test_submit_uses_category_subdirectory_and_completes_quickly():
    manager, qbt, announcer = make_manager()

    async def run():
        task = manager.submit("magnet:?xt=urn:btih:abc", "Movie")
        await task

    asyncio.run(run())

    assert qbt.add_calls == [
        {
            "urls": "magnet:?xt=urn:btih:abc",
            "save_path": os.path.join("/media/downloads", "Movie"),
        }
    ]
    assert announcer.submitted == [("Test Torrent", "Movie", None, None)]
    assert announcer.completed == [("Test Torrent", "Movie", None, None)]
    assert announcer.all_done_count == 1
    assert qbt.torrents == []  # completed torrent was deleted


def test_actor_rides_along_on_the_submitted_and_completed_events():
    manager, _, announcer = make_manager()

    async def run():
        await manager.submit("magnet:?xt=urn:btih:abc", "Movie", actor="deckhand")

    asyncio.run(run())

    assert announcer.submitted == [("Test Torrent", "Movie", "deckhand", None)]
    assert announcer.completed == [("Test Torrent", "Movie", "deckhand", None)]


def test_completion_sweep_deletes_only_hashes_this_manager_submitted():
    qbt = FakeQbt()
    # Torrents that existed before this manager started must be left alone,
    # even when they are already completed.
    qbt.add_torrent("foreign-completed", "Someone Else's Done Torrent",
                    status="completed")
    qbt.add_torrent("foreign-downloading", "Someone Else's Active Torrent",
                    eta=60, status="downloading")
    manager, _, announcer = make_manager(qbt=qbt)

    async def run():
        task = manager.submit("magnet:?xt=urn:btih:abc", "TV Show")
        await task

    asyncio.run(run())

    hashes = {t.hash for t in qbt.torrents}
    assert "foreign-completed" in hashes
    assert "foreign-downloading" in hashes
    assert ("Test Torrent", "TV Show", None, None) in announcer.completed
    assert all(
        name != "Someone Else's Done Torrent" for name, *_ in announcer.completed
    )


def test_all_done_announced_when_nothing_active():
    qbt = FakeQbt()
    qbt.add_torrent("foreign", "Foreign Active Torrent", eta=60,
                    status="downloading")
    manager, _, announcer = make_manager(qbt=qbt)

    async def run():
        task = manager.submit("magnet:?xt=urn:btih:abc", "Other")
        await task

    asyncio.run(run())

    assert announcer.all_done_count == 1
    # The foreign downloading torrent is still going; manager stops polling
    # for its own torrent but does not touch the foreign one.
    assert [t.hash for t in qbt.torrents] == ["foreign"]


def test_all_done_announced_once_only_after_all_concurrent_submits_finish():
    """all_done is a manager-level drain event, not a per-task event: with two
    submits in flight, the first finisher must not announce while the second
    is still downloading."""
    manager, qbt, announcer = make_manager(poll_interval=0.01)

    async def run():
        t1 = manager.submit("magnet:?xt=urn:btih:one", "Movie")
        t2 = manager.submit("magnet:?xt=urn:btih:two", "TV Show")
        await asyncio.gather(t1, t2)

    asyncio.run(run())

    assert len(announcer.completed) == 2
    assert announcer.all_done_count == 1
    assert qbt.torrents == []  # both completed torrents were swept


def test_completed_events_keep_each_torrents_own_actor():
    qbt = FakeQbt()
    manager, _, announcer = make_manager(qbt=qbt, poll_interval=0.01)

    async def run():
        t1 = manager.submit("magnet:?xt=urn:btih:one", "Movie", actor="first")
        t2 = manager.submit("magnet:?xt=urn:btih:two", "TV Show", actor="second")
        await asyncio.gather(t1, t2)

    asyncio.run(run())

    assert {actor for _, _, actor, _ in announcer.completed} == {"first", "second"}
    # No attribution rows linger after everything is swept.
    assert manager._actors == {}


def test_all_done_rearms_for_a_later_submit():
    manager, qbt, announcer = make_manager(poll_interval=0.01)

    async def run():
        await manager.submit("magnet:?xt=urn:btih:a", "Movie")
        await manager.submit("magnet:?xt=urn:btih:b", "Other")

    asyncio.run(run())

    # One all-done announcement per drain.
    assert announcer.all_done_count == 2


def test_failed_add_does_not_monitor_or_announce():
    manager, qbt, announcer = make_manager()
    qbt.add_error = RuntimeError("qbt down")

    async def run():
        task = manager.submit("magnet:?xt=urn:btih:abc", "Movie")
        await task

    asyncio.run(run())

    assert announcer.completed == []
    assert announcer.all_done_count == 0
    assert qbt.delete_calls == []


def test_cancel_all_deletes_every_torrent_and_reports_names():
    qbt = FakeQbt()
    qbt.add_torrent("h1", "First", status="downloading")
    qbt.add_torrent("h2", "Second", status="completed")
    manager, _, _ = make_manager(qbt=qbt)

    cancelled = manager.cancel_all()

    assert sorted(cancelled) == ["First", "Second"]
    assert qbt.torrents == []
