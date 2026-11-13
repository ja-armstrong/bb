"""The Bot's Discord message flows.

The Bot is a thin Frontend (ADR-0001): every flow relays the acting
Discord user's ID and display name to the API through
``bot.api_client.ApiClient`` — the Bot never touches qBittorrent,
Jackett or the Download Manager itself.

A message is dispatched by shape:

- a pasted magnet link -> the shared CategorySelectView, submitted
  through the API;
- ``progress`` -> the Actor's In-flight Downloads, read from the
  Actor-scoped ``GET /media`` listing;
- ``cancel`` -> pick one In-flight Download to cancel through the
  Download-level ``POST /media/{id}/cancel`` (the old
  cancel-everything behavior is intentionally gone);
- anything else -> a Search through the API, shown as a paginated
  result view whose selection leads to the category picker.

A failed API call is relayed to the channel with the API's own
``detail`` — the quota-exceeded message is written for the Actor, and
the Bot's job is to carry it across, not to reword it.

``attach`` needs only a ``client.event`` decorator and a ``user``; the
tests register a stand-in and drive ``on_message`` with fake messages,
so the Discord network layer stays out of the test path.
"""

import logging

from .discord_ui import CancelSelectView, CategorySelectView, SearchResultsView

logger = logging.getLogger(__name__)


def error_text(result) -> str:
    """One user-facing line for a failed API call.

    The API's ``detail`` is addressed to the Actor (the quota-exceeded
    rejection tells them what to do about it); anything else — including
    a transport failure surfaced as a 503 — becomes a generic failure
    with the status code.
    """
    payload = result.payload
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return f"API error {result.status_code}."


def actor_of(message):
    """The acting Discord user's identity, relayed on every API call."""
    return str(message.author.id), message.author.display_name


def make_submitter(api, discord_id, discord_name):
    """A submit callable for the category picker views.

    Closes over the acting Discord user. A rejected submission (quota
    exceeded, bad Category, unreachable API) raises with the API's own
    detail, which the view relays through its error path.
    """
    async def submit(magnet_link, category, *, size_bytes=0, title=None):
        result = api.submit(
            magnet_link,
            category,
            size_bytes=size_bytes,
            title=title,
            discord_id=discord_id,
            discord_name=discord_name,
        )
        if result.status_code >= 400:
            raise RuntimeError(error_text(result))
        return result

    return submit


def make_canceller(api, discord_id, discord_name):
    """A cancel callable for the CancelSelectView, scoped to the Actor."""
    async def cancel(download_id):
        result = api.media_cancel(
            download_id, discord_id=discord_id, discord_name=discord_name
        )
        if result.status_code >= 400:
            raise RuntimeError(error_text(result))
        return result

    return cancel


def in_flight(downloads):
    """The In-flight rows of a ``/media`` listing."""
    return [d for d in downloads if d.get("status") == "in_flight"]


async def fetch_media_or_send_error(message, api):
    """The Actor's ``/media`` listing, or ``None`` with the error sent."""
    discord_id, discord_name = actor_of(message)
    result = api.media(discord_id=discord_id, discord_name=discord_name)
    if result.status_code != 200:
        await message.channel.send(error_text(result))
        return None
    return (result.payload or {}).get("downloads", [])


async def submit_magnet(message, api):
    """A pasted magnet link: Category picker, then submit via the API."""
    discord_id, discord_name = actor_of(message)
    view = CategorySelectView(
        message.content.strip(),
        make_submitter(api, discord_id, discord_name),
    )
    await message.channel.send("Pick a Category for that magnet:", view=view)


async def run_search(message, api, query):
    """Anything else is a Search: paginated results through the API."""
    discord_id, discord_name = actor_of(message)
    result = api.search(query, discord_id=discord_id, discord_name=discord_name)
    if result.status_code != 200:
        await message.channel.send(error_text(result))
        return
    results = (result.payload or {}).get("results", [])
    if not results:
        await message.channel.send(f"No results for '{query}'.")
        return
    view = SearchResultsView(
        results, make_submitter(api, discord_id, discord_name)
    )
    await message.channel.send(f"Results for '{query}':", view=view)


async def show_progress(message, api):
    """The Actor's In-flight Downloads, from their own ``/media`` rows."""
    downloads = await fetch_media_or_send_error(message, api)
    if downloads is None:
        return
    in_flight_rows = in_flight(downloads)
    if not in_flight_rows:
        await message.channel.send("You have no In-flight Downloads.")
        return
    lines = [
        "{name} — {progress} (ETA {eta})".format(
            name=row.get("name") or "(unnamed)",
            progress=row.get("progress") or "…",
            eta=row.get("eta") or "…",
        )
        for row in in_flight_rows
    ]
    await message.channel.send(
        "In-flight Downloads:\n" + "\n".join(lines)
    )


async def show_cancel(message, api):
    """Cancel one owned In-flight Download, picked from a menu."""
    downloads = await fetch_media_or_send_error(message, api)
    if downloads is None:
        return
    in_flight_rows = in_flight(downloads)
    if not in_flight_rows:
        await message.channel.send(
            "You have no In-flight Downloads to cancel."
        )
        return
    discord_id, discord_name = actor_of(message)
    view = CancelSelectView(
        in_flight_rows, make_canceller(api, discord_id, discord_name)
    )
    await message.channel.send(
        "Which Download should be cancelled?", view=view
    )


def attach(client, api):
    """Wire the message flows onto a Discord client.

    The Bot replies to messages addressed to it or to the configured
    channel: magnets, the two commands, and any other text treated as a
    Search. Bot-authored messages and its own are ignored, and a broken
    flow is logged rather than taking the listener down.
    """
    @client.event
    async def on_message(message):
        if getattr(message.author, "bot", False):
            return
        if client.user is not None and message.author.id == client.user.id:
            return
        content = (message.content or "").strip()
        if not content:
            return
        try:
            lowered = content.lower()
            if lowered.startswith("magnet:?"):
                await submit_magnet(message, api)
            elif lowered == "progress":
                await show_progress(message, api)
            elif lowered == "cancel":
                await show_cancel(message, api)
            else:
                await run_search(message, api, content)
        except Exception:
            logger.exception("Discord message flow failed.")
