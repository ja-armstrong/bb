"""Tests for the Bot's Completion Event webhook receiver (ticket #3).

The Download Manager mirrors Completion Events here; the Bot announces them
to DISCORD_ANNOUNCE_CHANNEL_ID when its Discord client is connected.
"""

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
