"""Tests for Lifecycle Event emission (manager.events) and the full
submit -> submitted/completed/drained flow through the Download Manager."""

import asyncio

from manager.config import Config
from manager.downloads import DownloadManager
from manager.events import WebhookAnnouncer

API_URL = "http://api.test"
BOT_URL = "http://bot.test"
TOKEN = "service-token"


class RecordingTransport:
    """Records every webhook it is asked to POST."""

    def __init__(self, fail_urls=()):
        self.calls = []
        self.fail_urls = fail_urls

    def __call__(self, url, headers, payload):
        if any(marker in url for marker in self.fail_urls):
            raise ConnectionError(f"cannot reach {url}")
        self.calls.append(
            {"url": url, "headers": headers, "payload": payload}
        )

    @property
    def events(self):
        return [call["payload"]["event"] for call in self.calls]

    def payloads(self, containing):
        return [
            call["payload"] for call in self.calls if containing in call["url"]
        ]


def make_config():
    return Config(
        qbit_username="admin",
        qbit_password="hunter2",
        base_download_dir="/media/downloads",
        jackett_api_key="jackett-key",
        service_token=TOKEN,
    )


class FakeTorrent:
    def __init__(self, hash, name, status="downloading"):
        self.hash = hash
        self.name = name
        self.progress = 0.5
        self.eta = 3600
        self.status = status
        self.save_path = None


class FakeQbt:
    """A one-torrent stand-in: the added torrent completes on poll two."""

    def __init__(self):
        self.torrents = []
        self.add_calls = []
        self.delete_calls = []
        self._next_hash = 0

    def add_torrent(self, hash, name, status="downloading"):
        self.torrents.append(FakeTorrent(hash, name, status))

    def torrents_add(self, urls=None, save_path=None):
        self.add_calls.append({"urls": urls, "save_path": save_path})
        self._next_hash += 1
        self.torrents.append(FakeTorrent(f"hash{self._next_hash}", "Test Torrent"))
        return "Ok."

    def torrents_info(self, status_filter=None):
        # The torrent finishes after its first poll cycle.
        for torrent in self.torrents:
            if torrent.status == "downloading" and torrent.hash.startswith("hash"):
                torrent.status = "completed"
        if status_filter is None:
            return list(self.torrents)
        return [t for t in self.torrents if t.status == status_filter]

    def torrents_delete(self, torrent_hashes=None):
        self.delete_calls.append(torrent_hashes)
        self.torrents = [t for t in self.torrents if t.hash != torrent_hashes]


def make_announcer(transport, bot_base_url=BOT_URL):
    return WebhookAnnouncer(
        api_base_url=API_URL,
        service_token=TOKEN,
        bot_base_url=bot_base_url,
        transport=transport,
    )


def test_every_lifecycle_event_is_posted_to_the_api():
    transport = RecordingTransport()
    announcer = make_announcer(transport)

    async def run():
        await announcer.announce_submitted("Some Movie", "Movie", actor="deckhand")
        await announcer.announce_completed("Some Movie", "Movie", actor="deckhand")
        await announcer.announce_all_done()

    asyncio.run(run())

    api_events = transport.payloads(f"{API_URL}/internal/events")
    assert [p["event"] for p in api_events] == [
        "submitted",
        "completed",
        "drained",
    ]


def test_events_carry_the_actor_torrent_name_and_category():
    transport = RecordingTransport()
    announcer = make_announcer(transport)

    async def run():
        await announcer.announce_submitted("Some Movie", "Movie", actor="deckhand")

    asyncio.run(run())

    payload = transport.calls[0]["payload"]
    assert payload == {
        "event": "submitted",
        "torrent_name": "Some Movie",
        "category": "Movie",
        "actor": "deckhand",
        "download_id": None,
    }


def test_completion_events_are_mirrored_to_the_bot_webhook():
    transport = RecordingTransport()
    announcer = make_announcer(transport)

    async def run():
        await announcer.announce_submitted("Some Movie", "Movie")
        await announcer.announce_completed("Some Movie", "Movie")
        await announcer.announce_all_done()

    asyncio.run(run())

    bot_events = transport.payloads(f"{BOT_URL}/internal/webhook/completion")
    # Only the Completion Event reaches the Bot.
    assert [p["event"] for p in bot_events] == ["completed"]


def test_events_carry_the_bearer_token():
    transport = RecordingTransport()
    announcer = make_announcer(transport)

    async def run():
        await announcer.announce_completed("Some Movie", "Movie")

    asyncio.run(run())

    headers = transport.calls[0]["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_dead_api_sink_does_not_break_the_bot_mirror_or_raise():
    transport = RecordingTransport(fail_urls=("/internal/events",))
    announcer = make_announcer(transport)

    async def run():
        await announcer.announce_completed("Some Movie", "Movie")

    asyncio.run(run())

    # The API sink failed but the Bot mirror still went out.
    assert len(transport.payloads(f"{BOT_URL}/internal/webhook/completion")) == 1


def test_a_dead_bot_sink_does_not_raise():
    transport = RecordingTransport(fail_urls=("bot.test",))
    announcer = make_announcer(transport)

    async def run():
        await announcer.announce_completed("Some Movie", "Movie")

    asyncio.run(run())

    assert len(transport.payloads("/internal/events")) == 1


def test_bot_mirror_is_skipped_without_a_bot_url():
    transport = RecordingTransport()
    announcer = make_announcer(transport, bot_base_url=None)

    async def run():
        await announcer.announce_completed("Some Movie", "Movie")

    asyncio.run(run())

    assert len(transport.calls) == 1
    assert transport.calls[0]["url"] == f"{API_URL}/internal/events"


def test_full_lifecycle_through_the_manager_emits_submitted_completed_drained():
    """The acceptance-criterion path: one submitted Download produces all
    three Lifecycle Events, to the API and (for completion) the Bot."""
    transport = RecordingTransport()
    announcer = make_announcer(transport)
    qbt = FakeQbt()
    manager = DownloadManager(qbt, make_config(), announcer, poll_interval=0.01)

    async def run():
        task = manager.submit("magnet:?xt=urn:btih:abc", "Movie", actor="deckhand")
        await task

    asyncio.run(run())

    api_events = [p["event"] for p in transport.payloads("/internal/events")]
    assert api_events == ["submitted", "completed", "drained"]

    submitted = transport.payloads("/internal/events")[0]
    assert submitted["actor"] == "deckhand"
    assert submitted["torrent_name"] == "Test Torrent"

    bot_events = [p["event"] for p in transport.payloads("bot.test")]
    assert bot_events == ["completed"]
