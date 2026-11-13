"""The Bitbeard Bot application.

The Bot is a Frontend: it authenticates to the API as a Service Account and
relays the acting Discord user. In this scaffold it runs the Discord client
(when a token is configured) alongside a small HTTP listener that later
receives Completion Event webhooks. Without a token it stays up and idles, so
the Subsystem can start before Discord credentials exist.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import discord
import uvicorn
from fastapi import FastAPI

from .config import Config, load_config

logger = logging.getLogger(__name__)

BOT_PORT = 9872


async def _run_discord(config: Config) -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info("%s has connected to Discord.", client.user)

    try:
        await client.start(config.discord_bot_token)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Discord client stopped.")


def create_app(config: Config | None = None) -> FastAPI:
    """Build the Bot app. ``config`` is injectable for tests."""
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if config.discord_bot_token:
            task = asyncio.create_task(_run_discord(config))
            app.state.discord_task = task
        else:
            logger.warning(
                "DISCORD_BOT_TOKEN not set; Bot is idling without Discord."
            )
        yield
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Bitbeard Bot", lifespan=lifespan)
    app.state.config = config

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "bot"}

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=BOT_PORT)
