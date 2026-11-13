"""The Bitbeard Bot application.

The Bot is a Frontend: it authenticates to the API as a Service Account and
relays the acting Discord user. It runs the Discord client (when a token is
configured) whose message flows — search, magnet submission, cancel and
progress — all go through the API (``bot.discord_front``, ADR-0002),
alongside a small HTTP listener that receives the Completion Event webhooks
the Download Manager mirrors to it (ADR-0001), announcing them to the
configured Discord channel.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import discord
import uvicorn
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from shared.service_auth import require_service_token

from . import discord_front
from .api_client import ApiClient
from .config import Config, load_config
from .notifier import CompletionNotifier, NullNotifier

logger = logging.getLogger(__name__)

BOT_PORT = 9872


class CompletionEvent(BaseModel):
    """One Lifecycle Event as the manager mirrors it; only completed ones
    are announced."""

    event: str
    torrent_name: str | None = None
    category: str | None = None
    actor: str | None = None
    #: The Download's ownership-record id, carried for traceability.
    download_id: int | None = None


def _build_discord_client(config: Config) -> discord.Client:
    """Build the Discord client the Bot runs when a token is configured.

    The message flows are the Bot's Frontend surface: they relay the
    acting Discord user to the API through an ApiClient built from the
    same config, so search, submit, cancel and progress all work
    through the API (ADR-0002).
    """
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    discord_front.attach(
        client, ApiClient(config.api_url, config.service_token)
    )

    @client.event
    async def on_ready():
        logger.info("%s has connected to Discord.", client.user)

    return client


async def _run_discord(client: discord.Client, token: str) -> None:
    try:
        await client.start(token)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Discord client stopped.")


def create_app(config: Config | None = None, notifier=None) -> FastAPI:
    """Build the Bot app. ``config`` and ``notifier`` are injectable for tests.

    Without a notifier, one is built from the config: a real one wired to a
    fresh Discord client when credentials exist, a silent stand-in when they
    do not (the Bot stays up so the Optional Subsystem can start first).
    """
    config = config or load_config()

    discord_client = None
    if notifier is None:
        if config.discord_bot_token and config.announce_channel_id:
            discord_client = _build_discord_client(config)
            notifier = CompletionNotifier(
                discord_client, config.announce_channel_id
            )
        else:
            logger.warning(
                "DISCORD_BOT_TOKEN/DISCORD_ANNOUNCE_CHANNEL_ID not fully set; "
                "Bot is idling without Discord."
            )
            notifier = NullNotifier()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if discord_client is not None:
            task = asyncio.create_task(
                _run_discord(discord_client, config.discord_bot_token)
            )
            app.state.discord_task = task
        yield
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Bitbeard Bot", lifespan=lifespan)
    app.state.config = config
    app.state.notifier = notifier

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "bot"}

    @app.post(
        "/internal/webhook/completion",
        dependencies=[
            Depends(require_service_token(config.service_token))
        ],
    )
    async def completion_webhook(event: CompletionEvent):
        """Receive a mirrored Lifecycle Event and announce completions.

        Every event is accepted (the manager fails soft either way) but
        only Completion Events are announced, and only when Discord is
        actually connected.
        """
        if event.event != "completed":
            return {"announced": False}
        if not app.state.notifier.available:
            logger.info(
                "Completion Event for %r arrived while Discord is "
                "unconfigured; not announced.",
                event.torrent_name,
            )
            return {"announced": False}
        await app.state.notifier.announce(event.torrent_name, event.category)
        return {"announced": True}

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=BOT_PORT)
