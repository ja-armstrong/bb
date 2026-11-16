"""The reusable views for the Discord adapter.

Both places that ask the user to pick a download category (magnet paste in
on_message and result selection in SearchResultsView) build CategorySelectView
with their own submit callable and deliver the prompt their own way. The view
owns the interaction-ack dance exactly once: defer, submit, confirm, delete the
prompt message, and report errors through whichever channel is still live.

SearchResultsView and CancelSelectView are the ticket #7 result/cancel
menus; they feed the same CategorySelectView and the same submit/cancel
callables the message flows build (bot.discord_front).
"""
import logging
import math

import discord
from discord.ui import Button, Select, View


CATEGORY_OPTIONS = [
    # USER-FACING
    discord.SelectOption(label="Movie", emoji="🎬"),
    # USER-FACING
    discord.SelectOption(label="TV Show", emoji="📺"),
    # USER-FACING
    discord.SelectOption(label="Other", emoji="📁"),
]


async def send_error(interaction, text):
    """Send an error message for an interaction, wherever it is in its lifecycle."""
    if not interaction.response.is_done():
        await interaction.response.send_message(text, ephemeral=True)
    else:
        await interaction.followup.send(text, ephemeral=True)


class CategorySelectView(View):
    """A category picker that hands (magnet_link, category) to a submit callable.

    The view is agnostic about how the prompt is delivered: callers construct
    it and send it themselves — a normal channel message or an ephemeral
    followup, whichever suits the entry point.
    """

    def __init__(self, magnet_link, submit):
        super().__init__(timeout=300)
        self.magnet_link = magnet_link
        self.submit = submit
        self._select = Select(
            # USER-FACING
            placeholder="Choose a category", options=CATEGORY_OPTIONS
        )
        self._select.callback = self.choose_category
        self.add_item(self._select)

    async def choose_category(self, interaction):
        category = self._select.values[0]
        try:
            # Acknowledge the interaction first
            await interaction.response.defer()
            # Start the download (non-blocking)
            await self.submit(self.magnet_link, category)
            await interaction.channel.send(
                # USER-FACING
                f"Magnet link added to qBittorrent under {category}."
            )
            # Delete the category selection message
            await interaction.message.delete()
        except Exception as e:
            logging.error(f"Category selection failed: {e}")
            try:
                # USER-FACING
                await send_error(interaction, f"Error: {str(e)}")
            except Exception as send_failure:
                logging.error(
                    f"Failed to send category-selection error message: "
                    f"{send_failure}"
                )


class SearchResultsView(View):
    """One page of Torznab results as a Select, with Prev/Next paging.

    The Bot's Search flow builds this from the API's results and a submit
    callable taking ``(magnet_link, category, *, size_bytes, title)``.
    Choosing a result opens the shared CategorySelectView as a fresh
    prompt; the chosen result's size and title ride along so the API's
    Storage Quota pre-check and the Download's ownership record get the
    real numbers.
    """

    #: A Select may hold at most 25 options (Discord's hard cap).
    PAGE_SIZE = 25

    def __init__(self, results, submit):
        super().__init__(timeout=300)
        self.results = results
        self.submit = submit
        self.page = 0
        # USER-FACING
        self._select = Select(placeholder="Choose a result")
        self._select.callback = self.choose_result
        self.add_item(self._select)
        # USER-FACING
        self._prev = Button(label="Prev", style=discord.ButtonStyle.secondary)
        self._prev.callback = self.prev_page
        self.add_item(self._prev)
        # USER-FACING
        self._next = Button(label="Next", style=discord.ButtonStyle.secondary)
        self._next.callback = self.next_page
        self.add_item(self._next)
        self.refresh_options()

    def page_count(self) -> int:
        return max(1, math.ceil(len(self.results) / self.PAGE_SIZE))

    def refresh_options(self) -> None:
        """Rebuild the Select for the current page (values index ``results``)."""
        start = self.page * self.PAGE_SIZE
        self._select.options = [
            discord.SelectOption(
                # USER-FACING
                label=(result.get("title") or "(untitled result)")[:100],
                description=(
                    # USER-FACING
                    f"{float(result.get('size_gb') or 0):.2f} GB | "
                    f"{result.get('seeders') or 0} seeders"
                )[:100],
                value=str(start + index),
            )
            for index, result in enumerate(
                self.results[start:start + self.PAGE_SIZE]
            )
        ]
        self._select.placeholder = (
            # USER-FACING
            f"Choose a result (page {self.page + 1} of {self.page_count()})"
        )
        self._prev.disabled = self.page == 0
        self._next.disabled = self.page >= self.page_count() - 1

    async def choose_result(self, interaction):
        result = self.results[int(self._select.values[0])]

        async def submit_bound(magnet_link, category):
            # The Torznab size and title ride along: the quota pre-check
            # and the ownership record need the real numbers, exactly as
            # the Webui's search flow sends them.
            await self.submit(
                magnet_link,
                category,
                size_bytes=int(
                    round(float(result.get("size_gb") or 0) * (1024 ** 3))
                ),
                title=result.get("title"),
            )

        await interaction.response.send_message(
            # USER-FACING
            "Pick a Category:",
            view=CategorySelectView(result["magnet_link"], submit_bound),
        )

    async def prev_page(self, interaction):
        if self.page > 0:
            self.page -= 1
            self.refresh_options()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction):
        if self.page < self.page_count() - 1:
            self.page += 1
            self.refresh_options()
        await interaction.response.edit_message(view=self)


class CancelSelectView(View):
    """Pick one of the Actor's In-flight Downloads to cancel.

    The Bot's cancel flow builds this from the In-flight rows of the
    Actor's own ``/media`` listing; the chosen Download is cancelled
    through the API's Download-level ``/media/{id}/cancel``. The old
    cancel-everything behavior is intentionally gone: cancelling is
    scoped to a single owned Download, picked by the Actor.
    """

    def __init__(self, downloads, cancel):
        super().__init__(timeout=300)
        self.cancel = cancel
        # USER-FACING
        self._select = Select(placeholder="Cancel which Download?")
        for download in downloads:
            self._select.add_option(
                label=(
                    # USER-FACING
                    f"#{download['id']} {download.get('name') or '(unnamed)'}"
                )[:100],
                value=str(download["id"]),
                # USER-FACING
                description=(download.get("progress") or "…")[:100],
            )
        self._select.callback = self.cancel_chosen
        self.add_item(self._select)

    async def cancel_chosen(self, interaction):
        download_id = int(self._select.values[0])
        try:
            await interaction.response.defer()
            await self.cancel(download_id)
            # USER-FACING
            await interaction.channel.send("Download cancelled.")
            await interaction.message.delete()
        except Exception as e:
            logging.error(f"Cancel selection failed: {e}")
            try:
                await send_error(interaction, f"Error: {str(e)}")
            except Exception as send_failure:
                logging.error(
                    f"Failed to send cancel error message: {send_failure}"
                )
