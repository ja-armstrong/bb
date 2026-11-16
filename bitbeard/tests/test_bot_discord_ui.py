"""Tests for the reusable views (bot.discord_ui): the category picker,
the paginated search results (ticket #7) and the cancel menu."""
import asyncio

import discord
from discord.ui import Select

from bot.discord_ui import (
    CancelSelectView,
    CategorySelectView,
    SearchResultsView,
)


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
        self.views = []

    async def send(self, content, view=None):
        self.sent.append(content)
        self.views.append(view)


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


class RecordingKwargsSubmit:
    """Records what SearchResultsView's bound submit hands to the picker."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, magnet_link, category, *, size_bytes=0, title=None):
        self.calls.append((magnet_link, category, size_bytes, title))
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


def test_view_has_exactly_the_four_expected_options():
    view, _, _ = make_view()

    selects = [item for item in view.children if isinstance(item, Select)]
    assert len(selects) == 1

    options = selects[0].options
    assert [(o.label, str(o.emoji)) for o in options] == [
        ("Movie", "🎬"),
        ("TV Show", "📺"),
        ("Other", "📁"),
        ("Audiobook", "🎧"),
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


# --- SearchResultsView (ticket #7) -----------------------------------------


def make_results(count):
    return [
        {
            "title": f"Result {index}",
            "magnet_link": f"magnet:?xt=urn:btih:{index}",
            "seeders": 10 + index,
            "size_gb": 1.5,
        }
        for index in range(count)
    ]


def make_search_view(results=None, submit=None):
    submit = submit if submit is not None else RecordingKwargsSubmit()
    view = SearchResultsView(results or make_results(30), submit)
    select = [item for item in view.children if isinstance(item, Select)][0]
    return view, select, submit


def test_search_view_pages_25_results_with_title_size_and_seeders():
    view, select, _ = make_search_view()

    assert len(select.options) == 25
    first = select.options[0]
    assert first.label == "Result 0"
    assert first.description == "1.50 GB | 10 seeders"
    assert first.value == "0"
    assert "page 1 of 2" in select.placeholder


def test_search_view_prev_and_next_walk_the_pages():
    async def run():
        view, select, _ = make_search_view()
        interaction = FakeInteraction()

        await view.next_page(interaction)

        assert len(interaction.response.edited_views) == 1
        assert len(select.options) == 5
        assert select.options[0].value == "25"
        assert "page 2 of 2" in select.placeholder
        assert view._prev.disabled is False
        assert view._next.disabled is True

        await view.prev_page(FakeInteraction())

        assert len(select.options) == 25
        assert select.options[0].value == "0"

    asyncio.run(run())


def test_search_view_prev_stays_on_the_first_page():
    async def run():
        view, _, _ = make_search_view()
        interaction = FakeInteraction()

        await view.prev_page(interaction)

        # Already at the first page: the view is re-sent unchanged.
        assert len(interaction.response.edited_views) == 1
        assert view.page == 0

    asyncio.run(run())


def test_choosing_a_result_opens_the_category_picker_for_it():
    async def run():
        view, select, _ = make_search_view(results=make_results(30))
        interaction = FakeInteraction()
        select._values = ["27"]

        await view.choose_result(interaction)

        assert interaction.response.messages == ["Pick a category:"]
        prompt = interaction.response.views[0]
        assert isinstance(prompt, CategorySelectView)
        assert prompt.magnet_link == "magnet:?xt=urn:btih:27"

        # Picking a category in the prompt submits through the bound
        # callable; that round-trip is pinned by the next test.
        prompt_select = [
            item for item in prompt.children if isinstance(item, Select)
        ][0]
        prompt_interaction = FakeInteraction()
        choose(prompt_select, "TV Show")

        await prompt.choose_category(prompt_interaction)

    asyncio.run(run())


def test_the_bound_submit_carries_the_chosen_results_size_and_title():
    async def run():
        submit = RecordingKwargsSubmit()
        view, select, _ = make_search_view(
            results=make_results(30), submit=submit
        )
        interaction = FakeInteraction()
        select._values = ["3"]

        await view.choose_result(interaction)

        prompt = interaction.response.views[0]
        prompt_select = [
            item for item in prompt.children if isinstance(item, Select)
        ][0]
        prompt_interaction = FakeInteraction()
        choose(prompt_select, "Movie")

        await prompt.choose_category(prompt_interaction)

        size_bytes = int(1.5 * 1024 ** 3)
        assert submit.calls == [
            ("magnet:?xt=urn:btih:3", "Movie", size_bytes, "Result 3")
        ]

    asyncio.run(run())


# --- CancelSelectView (ticket #7) ------------------------------------------


class RecordingCancel:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, download_id):
        self.calls.append(download_id)
        if self.error is not None:
            raise self.error


def make_cancel_view(downloads=None, cancel=None):
    downloads = downloads or [
        {
            "id": 7,
            "name": "Some Movie",
            "status": "in_flight",
            "progress": "42.00%",
        },
        {"id": 8, "name": "Other", "status": "in_flight", "progress": None},
    ]
    cancel = cancel if cancel is not None else RecordingCancel()
    view = CancelSelectView(downloads, cancel)
    select = [item for item in view.children if isinstance(item, Select)][0]
    return view, select, cancel


def test_cancel_view_lists_the_given_in_flight_downloads():
    view, select, _ = make_cancel_view()

    assert [(o.label, o.value, o.description) for o in select.options] == [
        ("#7 Some Movie", "7", "42.00%"),
        ("#8 Other", "8", "…"),
    ]


def test_choosing_a_download_cancels_it_and_confirms():
    async def run():
        view, select, cancel = make_cancel_view()
        interaction = FakeInteraction()
        select._values = ["7"]

        await view.cancel_chosen(interaction)

        assert cancel.calls == [7]
        assert interaction.response.deferred
        assert interaction.channel.sent == ["Download cancelled."]
        assert interaction.message.delete_calls == 1

    asyncio.run(run())


def test_cancel_failure_reports_the_error_instead_of_confirming():
    async def run():
        view, select, cancel = make_cancel_view(
            cancel=RecordingCancel(error=RuntimeError("not In-flight anymore"))
        )
        interaction = FakeInteraction()
        select._values = ["8"]

        await view.cancel_chosen(interaction)

        assert interaction.channel.sent == []
        assert interaction.followup.messages == [
            "Error: not In-flight anymore"
        ]

    asyncio.run(run())
