"""The one reusable category-selection view for the Discord adapter.

Both places that ask the user to pick a download category (magnet paste in
on_message and result selection in SearchResultsView) build this view with
their own submit callable and deliver the prompt their own way. The view owns
the interaction-ack dance exactly once: defer, submit, confirm, delete the
prompt message, and report errors through whichever channel is still live.
"""
import logging

import discord
from discord.ui import Select, View


CATEGORY_OPTIONS = [
    discord.SelectOption(label="Movie", emoji="🎬"),
    discord.SelectOption(label="TV Show", emoji="📺"),
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
                f"Magnet link added to qBittorrent under {category}."
            )
            # Delete the category selection message
            await interaction.message.delete()
        except Exception as e:
            logging.error(f"Category selection failed: {e}")
            try:
                await send_error(interaction, f"Error: {str(e)}")
            except Exception as send_failure:
                logging.error(
                    f"Failed to send category-selection error message: "
                    f"{send_failure}"
                )
