"""Tests for the Bot's Completion Event webhook receiver (ticket #3).

The Download Manager mirrors Completion Events here; the Bot announces them
to DISCORD_ANNOUNCE_CHANNEL_ID when its Discord client is connected.
Announcing is optional (ticket #26): a Bot enabled with only a token runs
without announcements, skipping them quietly after one startup notice.
"""

import asyncio
import logging

from bot.config import Config
from bot.main import create_app
from fastapi.testclient import TestClient

TOKEN = "service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeDiscordClient:
    """Stand-in for discord.Client with one known channel."""

    def __init__(self, channel_id, channel):
        self._channel_id = channel_id
        self._channel = channel
        self.closed = False

    def is_closed(self):
        return self.closed

    def get_channel(self, channel_id):
        if channel_id == self._channel_id:
            return self._channel
        return None


class RecordingNotifier:
    """Injectable notifier that records what it is asked to announce."""

    def __init__(self, available=True):
        self.available_value = available
        self.announced = []

    @property
    def available(self):
        return self.available_value

    async def announce(self, torrent_name, category):
        self.announced.append((torrent_name, category))


def make_config(**overrides):
    values = dict(
        discord_bot_token="discord-token",
        announce_channel_id=123456789,
        api_url="http://127.0.0.1:9870",
        service_token=TOKEN,
    )
    values.update(overrides)
    return Config(**values)


def _post(client, payload, headers=AUTH):
    return client.post(
        "/internal/webhook/completion", json=payload, headers=headers
    )


COMPLETION = {
    "event": "completed",
    "torrent_name": "Some Movie",
    "category": "Movie",
    "actor": "deckhand",
}


def test_webhook_requires_the_service_token():
    notifier = RecordingNotifier()
    with TestClient(create_app(make_config(), notifier)) as client:
        assert _post(client, COMPLETION, headers={}).status_code == 401
        assert _post(
            client, COMPLETION, headers={"Authorization": "Bearer wrong"}
        ).status_code == 401

    assert notifier.announced == []


def test_webhook_announces_a_completion_to_the_discord_channel():
    channel = FakeChannel()
    client_stub = FakeDiscordClient(123456789, channel)
    notifier = RecordingNotifier()

    # RecordingNotifier ignores Discord; this test drives the real one to
    # prove the message lands in the channel.
    from bot.notifier import CompletionNotifier

    app = create_app(make_config(), CompletionNotifier(client_stub, 123456789))

    with TestClient(app) as client:
        response = _post(client, COMPLETION)

    assert response.status_code == 200
    assert response.json() == {"announced": True}
    assert channel.messages == [
        "Some Movie has been added to Bitbeard under Movie."
    ]


def test_webhook_skips_announcing_when_discord_is_not_connected():
    channel = FakeChannel()
    client_stub = FakeDiscordClient(123456789, channel)
    client_stub.closed = True

    from bot.notifier import CompletionNotifier

    app = create_app(make_config(), CompletionNotifier(client_stub, 123456789))

    with TestClient(app) as client:
        response = _post(client, COMPLETION)

    assert response.status_code == 200
    assert response.json() == {"announced": False}
    assert channel.messages == []


def test_webhook_accepts_events_when_discord_is_unconfigured():
    # No Discord token: the Bot idles, accepts the event, stays silent.
    config = make_config(discord_bot_token=None, announce_channel_id=None)

    with TestClient(create_app(config)) as client:
        response = _post(client, COMPLETION)

    assert response.status_code == 200
    assert response.json() == {"announced": False}


class StubBuiltDiscordClient:
    """Stand-in for the client create_app builds itself.

    start() blocks until cancelled, the way a live gateway connection
    does, so the app's lifespan has a task to run and to cancel.
    """

    def __init__(self):
        self.started = False
        self.closed = False
        self._connected = asyncio.Event()

    def is_closed(self):
        return self.closed

    def get_channel(self, channel_id):
        return None

    async def start(self, token):
        self.started = True
        await self._connected.wait()


def test_bot_runs_without_a_channel_and_skips_announcements(monkeypatch, caplog):
    # A token alone is enough to enable the Bot: the Discord client is
    # built and started, but no announcement is posted anywhere.
    stub = StubBuiltDiscordClient()
    monkeypatch.setattr("bot.main._build_discord_client", lambda config: stub)
    config = make_config(announce_channel_id=None)

    with caplog.at_level(logging.INFO, logger="bot.main"):
        with TestClient(create_app(config)) as client:
            response = _post(client, COMPLETION)

    assert response.status_code == 200
    assert response.json() == {"announced": False}
    assert stub.started

    # Exactly one announcements-off notice, at startup — nothing per
    # event, and no "unconfigured" complaint (the Bot is configured, only
    # the channel is absent).
    off = [r for r in caplog.records if "announcements are off" in r.getMessage()]
    assert len(off) == 1
    assert not [r for r in caplog.records if "unconfigured" in r.getMessage()]


def test_app_wires_an_announcing_notifier_when_a_channel_is_set(monkeypatch):
    # Token plus channel: the app builds its own client and a notifier
    # that announces, as before.
    stub = StubBuiltDiscordClient()
    monkeypatch.setattr("bot.main._build_discord_client", lambda config: stub)

    app = create_app(make_config())

    from bot.notifier import CompletionNotifier

    assert isinstance(app.state.notifier, CompletionNotifier)
    assert app.state.notifier.available


def test_completion_notifier_without_a_channel_skips_silently():
    from bot.notifier import CompletionNotifier

    notifier = CompletionNotifier(StubBuiltDiscordClient(), None)

    assert notifier.available is False
    asyncio.run(notifier.announce("Some Movie", "Movie"))  # no error, no send


def test_webhook_only_announces_completion_events():
    notifier = RecordingNotifier()

    with TestClient(create_app(make_config(), notifier)) as client:
        assert _post(client, {"event": "submitted", "torrent_name": "x"}).json() == {
            "announced": False
        }
        assert _post(client, {"event": "drained"}).json() == {"announced": False}

    assert notifier.announced == []


def test_unavailable_notifier_still_accepts_the_event():
    notifier = RecordingNotifier(available=False)

    with TestClient(create_app(make_config(), notifier)) as client:
        response = _post(client, COMPLETION)

    assert response.status_code == 200
    assert response.json() == {"announced": False}
    assert notifier.announced == []
