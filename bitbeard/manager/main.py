"""The Bitbeard Download Manager service.

Owns the torrent lifecycle, the qBittorrent client, search (Jackett/Torznab),
and all file operations on the Media Root. The API and Bot reach it with the
shared ``SERVICE_TOKEN``.
"""

from typing import Any

import qbittorrentapi
import uvicorn
from fastapi import Depends, FastAPI, Header, Query
from pydantic import BaseModel

from shared.service_auth import require_service_token

from .config import Config, load_config
from .downloads import DownloadManager
from .events import WebhookAnnouncer
from .search import search


class TorrentRequest(BaseModel):
    magnet_link: str
    category: str


#: Trusted headers the API sets on proxied calls (ADR-0002): the acting
#: Account's name and, when linked, the Discord name it acts for. The
#: manager does not interpret them — they ride along so Lifecycle Events
#: and (later) the downloads record stay attributable end to end.
ACTOR_HEADER = "X-Bitbeard-Account"
DISCORD_HEADER = "X-Bitbeard-Discord"


def make_qbt_client(config: Config) -> qbittorrentapi.Client:
    """Build the qBittorrent client.

    qBittorrent shares gluetun's network namespace, so localhost:8080 is
    always the right address. The client authenticates lazily on first use,
    which keeps startup independent of qBittorrent being ready.
    """
    return qbittorrentapi.Client(
        host="localhost",
        port=8080,
        username=config.qbit_username,
        password=config.qbit_password,
    )


def make_announcer(config: Config) -> WebhookAnnouncer:
    """Build the Lifecycle Event emitter from the service configuration."""
    return WebhookAnnouncer(
        api_base_url=config.api_url,
        service_token=config.service_token,
        bot_base_url=config.bot_url,
    )


def create_app(
    config: Config | None = None,
    qbt_client: Any | None = None,
    announcer: Any | None = None,
) -> FastAPI:
    """Build the manager app. Dependencies are injectable for tests."""
    config = config or load_config()
    app = FastAPI(title="Bitbeard Download Manager")
    app.state.config = config
    app.state.qbt_client = qbt_client or make_qbt_client(config)
    app.state.download_manager = DownloadManager(
        app.state.qbt_client,
        config,
        announcer or make_announcer(config),
    )

    service_auth = [Depends(require_service_token(config.service_token))]

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "manager"}

    @app.get("/search", dependencies=service_auth)
    async def api_search(q: str = Query(...)):
        results = search(q, config=config)
        return {"results": [result.as_dict() for result in results]}

    @app.post("/download", dependencies=service_auth)
    async def api_download(
        request: TorrentRequest,
        x_bitbeard_account: str | None = Header(default=None),
        x_bitbeard_discord: str | None = Header(default=None),
    ):
        app.state.download_manager.submit(
            request.magnet_link,
            request.category,
            actor=x_bitbeard_account or x_bitbeard_discord,
        )
        return {"message": "Download started successfully"}

    @app.get("/progress", dependencies=service_auth)
    async def api_progress():
        return {"progress": app.state.download_manager.progress()}

    @app.post("/cancel", dependencies=service_auth)
    async def api_cancel():
        return {"cancelled": app.state.download_manager.cancel_all()}

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=9871)
