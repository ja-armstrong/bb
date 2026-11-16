"""Tests for the Bot's Discord message flows (ticket #7).

The flows (bot.discord_front) are the Bot's Frontend surface: every one
relays the acting Discord user's ID and name to the API through the
injectable ``ApiClient`` transport, and every failed call is relayed to
the channel with the API's own detail. The tests drive the flows with a
fake Discord layer (plain stand-in message/channel objects attached via
``attach``) and a recording transport — discord.py's network layer
never runs.
"""

import asyncio

import pytest

from bot import discord_front
from bot.api_client import (
    DISCORD_ID_HEADER,
    DISCORD_NAME_HEADER,
    ApiClient,
    ApiResult,
)
from bot.discord_ui import CancelSelectView, CategorySelectView
from discord.ui import Select


class FakeAuthor:
    def __init__(self, id="123456789", name="Salty", bot=False):
        self.id = id
        self.display_name = name
        self.bot = bot


class FakeChannel:
    def __init__(self):
        self.sent = []  # (content, view) pairs

    async def send(self, content, view=None):
        self.sent.append((content, view))


class FakeMessage:
    def __init__(self, content, author=None):
        self.content = content
        self.author = author if author is not None else FakeAuthor()
        self.channel = FakeChannel()


class FakeSelf:
    id = "botself"


class FakeClient:
    """Stand-in for discord.Client: records the attached handlers."""

    def __init__(self):
        self.user = FakeSelf()
        self.handlers = {}

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn


class RecordingTransport:
    """Answers every call with the next canned result, recording it."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, method, url, *, headers, params=None, json=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        if self.results:
            return self.results.pop(0)
        return ApiResult(200, {})

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def transport():
    return RecordingTransport()


@pytest.fixture
def api(transport):
    return ApiClient("http://api.test", "service-token", transport=transport)


def select_of(view):
    return [item for item in view.children if isinstance(item, Select)][0]


def choose(select, value):
    # Select.values is a read-only property; the backing field is _values.
    select._values = [value]


def search_payload(count=1):
    return {
        "results": [
            {
                "title": f"Result {index}",
                "magnet_link": f"magnet:?xt=urn:btih:{index}",
                "seeders": 22,
                "size_gb": 2.0,
            }
            for index in range(count)
        ]
    }


# --- Search -----------------------------------------------------------------


def test_a_search_message_relays_the_query_and_the_actor(api, transport):
    transport.results = [ApiResult(200, search_payload())]
    message = FakeMessage("linux iso")

    asyncio.run(discord_front.run_search(message, api, "linux iso"))

    call = transport.last
    assert call["method"] == "GET"
    assert call["url"] == "http://api.test/search"
    assert call["params"] == {"q": "linux iso"}
    assert call["headers"]["Authorization"] == "Bearer service-token"
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    assert call["headers"][DISCORD_NAME_HEADER] == "Salty"
    content, view = message.channel.sent[0]
    assert content == "Results for 'linux iso':"
    assert "page 1 of 1" in select_of(view).placeholder


def test_a_search_with_thirty_results_pages_through_two(api, transport):
    transport.results = [ApiResult(200, search_payload(count=30))]
    message = FakeMessage("big query")

    asyncio.run(discord_front.run_search(message, api, "big query"))

    _, view = message.channel.sent[0]
    assert view.page_count() == 2
    assert len(select_of(view).options) == 25

    asyncio.run(view.next_page(FakeInteraction()))

    assert len(select_of(view).options) == 5
    assert select_of(view).options[0].value == "25"


def test_choosing_a_result_then_a_category_submits_through_the_api(
    api, transport
):
    transport.results = [ApiResult(200, search_payload())]
    message = FakeMessage("linux iso")
    asyncio.run(discord_front.run_search(message, api, "linux iso"))

    _, view = message.channel.sent[0]
    choose(select_of(view), "0")
    interaction = FakeInteraction()
    asyncio.run(view.choose_result(interaction))

    prompt = interaction.response.views[0]
    assert isinstance(prompt, CategorySelectView)
    choose(select_of(prompt), "Movie")
    prompt_interaction = FakeInteraction()
    asyncio.run(prompt.choose_category(prompt_interaction))

    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == "http://api.test/download"
    assert call["json"] == {
        "magnet_link": "magnet:?xt=urn:btih:0",
        "category": "Movie",
        # The Torznab size and title ride along for the quota pre-check
        # and the ownership record.
        "size_bytes": int(2.0 * 1024 ** 3),
        "title": "Result 0",
    }
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    assert call["headers"][DISCORD_NAME_HEADER] == "Salty"


def test_a_search_without_results_says_so(api, transport):
    transport.results = [ApiResult(200, {"results": []})]
    message = FakeMessage("dust")

    asyncio.run(discord_front.run_search(message, api, "dust"))

    assert transport.last["params"] == {"q": "dust"}
    assert message.channel.sent == [("No results for 'dust'.", None)]


def test_a_quota_exceeded_search_is_relayed_to_the_channel(api, transport):
    detail = (
        "Quota exceeded: this Download would put you at 60.00 GB of your "
        "50.00 GB limit. Delete something first."
    )
    transport.results = [ApiResult(403, {"detail": detail})]
    message = FakeMessage("huge movie")

    asyncio.run(discord_front.run_search(message, api, "huge movie"))

    assert message.channel.sent == [(detail, None)]


# --- Magnet submission ------------------------------------------------------


def test_a_pasted_magnet_opens_the_category_picker(api, transport):
    message = FakeMessage("magnet:?xt=urn:btih:abc")

    asyncio.run(discord_front.submit_magnet(message, api))

    content, view = message.channel.sent[0]
    assert content == "Pick a Category for that magnet:"
    assert isinstance(view, CategorySelectView)
    assert view.magnet_link == "magnet:?xt=urn:btih:abc"
    assert transport.calls == []


def test_choosing_a_category_submits_the_magnet_through_the_api(api, transport):
    message = FakeMessage("magnet:?xt=urn:btih:abc")
    asyncio.run(discord_front.submit_magnet(message, api))

    _, view = message.channel.sent[0]
    choose(select_of(view), "TV Show")
    interaction = FakeInteraction()
    asyncio.run(view.choose_category(interaction))

    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == "http://api.test/download"
    assert call["json"] == {
        "magnet_link": "magnet:?xt=urn:btih:abc",
        "category": "TV Show",
        "size_bytes": 0,
        "title": None,
    }
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    assert call["headers"][DISCORD_NAME_HEADER] == "Salty"


def test_choosing_audiobook_submits_the_magnet_through_the_api(
    api, transport
):
    message = FakeMessage("magnet:?xt=urn:btih:abc")
    asyncio.run(discord_front.submit_magnet(message, api))

    _, view = message.channel.sent[0]
    choose(select_of(view), "Audiobook")
    interaction = FakeInteraction()
    asyncio.run(view.choose_category(interaction))

    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == "http://api.test/download"
    assert call["json"] == {
        "magnet_link": "magnet:?xt=urn:btih:abc",
        "category": "Audiobook",
        "size_bytes": 0,
        "title": None,
    }
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    assert call["headers"][DISCORD_NAME_HEADER] == "Salty"


def test_a_quota_exceeded_submission_is_relayed(api, transport):
    detail = (
        "Quota exceeded: this Download would put you at 60.00 GB of your "
        "50.00 GB limit. Delete something first."
    )
    transport.results = [ApiResult(403, {"detail": detail})]
    message = FakeMessage("magnet:?xt=urn:btih:abc")
    asyncio.run(discord_front.submit_magnet(message, api))

    _, view = message.channel.sent[0]
    choose(select_of(view), "Movie")
    interaction = FakeInteraction()
    asyncio.run(view.choose_category(interaction))

    # The defer happened before the failing submit, so the API's own
    # quota message is relayed through the followup.
    assert interaction.channel.sent == []
    assert interaction.followup.messages == [f"Error: {detail}"]


# --- Progress ----------------------------------------------------------------


def make_media_payload():
    return {
        "downloads": [
            {
                "id": 1,
                "name": "Movie A",
                "status": "in_flight",
                "progress": "42.00%",
                "eta": "3m",
            },
            {
                "id": 2,
                "name": "Movie B",
                "status": "completed",
                "size_bytes": 1000,
            },
        ]
    }


def test_progress_lists_the_actors_in_flight_downloads(api, transport):
    transport.results = [ApiResult(200, make_media_payload())]
    message = FakeMessage("progress")

    asyncio.run(discord_front.show_progress(message, api))

    call = transport.last
    assert call["method"] == "GET"
    assert call["url"] == "http://api.test/media"
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    content, view = message.channel.sent[0]
    assert content.startswith("Downloads:")
    assert "Movie A — 42.00% (ETA 3m)" in content
    # Completed rows are not progress.
    assert "Movie B" not in content


def test_progress_with_nothing_in_flight_says_so(api, transport):
    transport.results = [
        ApiResult(200, {"downloads": [make_media_payload()["downloads"][1]]})
    ]
    message = FakeMessage("progress")

    asyncio.run(discord_front.show_progress(message, api))

    assert message.channel.sent == [("You have no downloads.", None)]


# --- Cancel ------------------------------------------------------------------


def test_cancel_offers_the_actors_in_flight_downloads_and_cancels_the_pick(
    api, transport
):
    transport.results = [ApiResult(200, make_media_payload())]
    message = FakeMessage("cancel")

    asyncio.run(discord_front.show_cancel(message, api))

    content, view = message.channel.sent[0]
    assert content == "Which download should be cancelled?"
    assert isinstance(view, CancelSelectView)
    # Only the In-flight Download is offered — and nothing was cancelled
    # merely by opening the menu.
    assert [o.value for o in select_of(view).options] == ["1"]
    assert transport.last["method"] == "GET"

    choose(select_of(view), "1")
    interaction = FakeInteraction()
    asyncio.run(view.cancel_chosen(interaction))

    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == "http://api.test/media/1/cancel"
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    assert call["headers"][DISCORD_NAME_HEADER] == "Salty"
    assert interaction.channel.sent == [("Download cancelled.", None)]


def test_cancel_with_nothing_in_flight_says_so(api, transport):
    transport.results = [ApiResult(200, {"downloads": []})]
    message = FakeMessage("cancel")

    asyncio.run(discord_front.show_cancel(message, api))

    assert message.channel.sent == [
        ("You have no downloads to cancel.", None)
    ]


# --- on_message dispatch -----------------------------------------------------


def test_on_message_dispatches_by_message_shape(api, transport):
    client = FakeClient()
    discord_front.attach(client, api)
    on_message = client.handlers["on_message"]

    magnet = FakeMessage("magnet:?xt=urn:btih:z")
    asyncio.run(on_message(magnet))
    assert isinstance(magnet.channel.sent[0][1], CategorySelectView)
    assert transport.calls == []

    asyncio.run(on_message(FakeMessage("progress")))
    assert transport.last["url"] == "http://api.test/media"

    asyncio.run(on_message(FakeMessage("CANCEL")))
    assert transport.last["url"] == "http://api.test/media"

    search = FakeMessage("some show")
    asyncio.run(on_message(search))
    assert transport.last["url"] == "http://api.test/search"
    assert transport.last["params"] == {"q": "some show"}


def test_on_message_ignores_bots_itself_and_empty_text(api, transport):
    client = FakeClient()
    discord_front.attach(client, api)
    on_message = client.handlers["on_message"]

    asyncio.run(on_message(FakeMessage("hi", author=FakeAuthor(bot=True))))
    asyncio.run(
        on_message(FakeMessage("hi", author=FakeAuthor(id="botself")))
    )
    asyncio.run(on_message(FakeMessage("   ")))

    assert transport.calls == []


# --- FakeInteraction (kept at the bottom: shared by the flow tests) ----------


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []
        self.views = []
        self.edited_views = []

    async def defer(self, ephemeral=False):
        self.deferred = True

    async def send_message(self, content, ephemeral=False, view=None):
        self.messages.append(content)
        self.views.append(view)

    async def edit_message(self, view=None):
        self.edited_views.append(view)

    def is_done(self):
        return self.deferred or bool(self.messages)


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append(content)


class FakePromptMessage:
    def __init__(self):
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = FakePromptMessage()
        self.channel = FakeChannel()
