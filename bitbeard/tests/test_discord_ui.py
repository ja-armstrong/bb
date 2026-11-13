"""Tests for the reusable category-select view (bitbeard.discord_ui)."""
import asyncio

import discord
from discord.ui import Select

from bitbeard.discord_ui import CategorySelectView


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    async def defer(self, ephemeral=False):
        self.deferred = True

    async def send_message(self, content, ephemeral=False):
        self.messages.append(content)

    def is_done(self):
        # After a real defer, the response is done.
        return self.deferred or bool(self.messages)


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append(content)


class FakeMessage:
    def __init__(self):
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content, view=None):
        self.sent.append(content)


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = FakeMessage()
        self.channel = FakeChannel()


class RecordingSubmit:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, magnet_link, category):
        self.calls.append((magnet_link, category))
        if self.error is not None:
            raise self.error


def make_view(magnet="magnet:?xt=urn:btih:abc", submit=None, error=None):
    submit = submit if submit is not None else RecordingSubmit(error=error)
    view = CategorySelectView(magnet, submit)
    select = [item for item in view.children if isinstance(item, Select)][0]
    return view, select, submit


def choose(select, value):
    # Select.values is a read-only property; the backing field is _values.
    select._values = [value]


def test_view_has_exactly_the_three_expected_options():
    view, _, _ = make_view()

    selects = [item for item in view.children if isinstance(item, Select)]
    assert len(selects) == 1

    options = selects[0].options
    assert [(o.label, str(o.emoji)) for o in options] == [
        ("Movie", "🎬"),
        ("TV Show", "📺"),
        ("Other", "📁"),
    ]


def test_view_times_out_after_300_seconds():
    view, _, _ = make_view()
    assert view.timeout == 300


def test_choosing_a_category_submits_magnet_and_chosen_category():
    async def run():
        view, select, submit = make_view()
        interaction = FakeInteraction()
        choose(select, "TV Show")

        await view.choose_category(interaction)

        assert submit.calls == [("magnet:?xt=urn:btih:abc", "TV Show")]
        assert interaction.response.deferred

    asyncio.run(run())


def test_view_message_deleted_after_submit():
    async def run():
        view, select, _ = make_view()
        interaction = FakeInteraction()
        choose(select, "Movie")

        await view.choose_category(interaction)

        assert interaction.message.delete_calls == 1

    asyncio.run(run())


def test_submit_failure_reports_via_followup_when_response_already_deferred():
    async def run():
        view, select, _ = make_view(error=RuntimeError("qbt down"))
        interaction = FakeInteraction()
        choose(select, "Other")

        await view.choose_category(interaction)

        # The defer happened before the failing submit, so the error must go
        # through the followup, not the (already-used) response.
        assert interaction.followup.messages == ["Error: qbt down"]
        assert interaction.response.messages == []

    asyncio.run(run())


def test_submit_failure_reports_via_response_when_not_yet_acknowledged():
    async def run():
        # A defer that blows up leaves the response unused, so the error must
        # take the send_message path.
        view, select, _ = make_view(error=RuntimeError("defer blew up"))
        interaction = FakeInteraction()
        interaction.response.defer = None  # defer itself fails
        choose(select, "Movie")

        await view.choose_category(interaction)

        assert interaction.response.messages == ["Error: 'NoneType' object is not callable"]
        assert interaction.followup.messages == []

    asyncio.run(run())


def test_confirmation_sent_to_channel_on_success():
    async def run():
        view, select, _ = make_view()
        interaction = FakeInteraction()
        choose(select, "Movie")

        await view.choose_category(interaction)

        assert interaction.channel.sent == [
            "Magnet link added to qBittorrent under Movie."
        ]

    asyncio.run(run())


def test_no_confirmation_when_submit_fails():
    async def run():
        view, select, _ = make_view(error=RuntimeError("boom"))
        interaction = FakeInteraction()
        choose(select, "Movie")

        await view.choose_category(interaction)

        assert interaction.channel.sent == []

    asyncio.run(run())
