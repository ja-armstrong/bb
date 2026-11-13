"""The Bitbeard Download Manager service.

Owns the torrent lifecycle, the qBittorrent client, search (Jackett/Torznab),
and all file operations on the Media Root. The API and Bot reach it with the
shared ``SERVICE_TOKEN``.

Ownership and Storage Quota live here too (the manager owns the
``downloads`` records and the per-Account usage counter): every submission
opens a record, the torrent lifecycle keeps it current, a periodic
reconciliation job recomputes the counters from the records vs on-disk
state, and ``POST /media/{id}/delete`` hard-deletes a finished Download
(files, record, quota) for the user-facing endpoint of ticket #6.
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

import qbittorrentapi
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from shared import db
from shared.service_auth import require_service_token

from .config import Config, load_config
from .downloads import DownloadManager
from .events import WebhookAnnouncer
from .quota import ActiveTorrentError, OutsideMediaRootError, QuotaLedger
from .search import search

logger = logging.getLogger(__name__)


class TorrentRequest(BaseModel):
    magnet_link: str
    category: str
    #: The projected size from the Torznab result, when the submitter had
    #: one; 0 means unknown (the record self-corrects at completion).
    size_bytes: int = 0
    #: The Torznab title, when known; qBittorrent's real name replaces it.
    title: str | None = None


#: Trusted headers the API sets on proxied calls (ADR-0002): the acting
#: Account's name (for Lifecycle Event attribution) and, when the Actor is
#: a linked Account, its id — which is what the ``downloads`` record names
#: as its owner. Unlinked submitters arrive without the id and are recorded
#: under the ``discord:<name>`` Actor instead.
ACTOR_HEADER = "X-Bitbeard-Account"
DISCORD_HEADER = "X-Bitbeard-Discord"
ACCOUNT_ID_HEADER = "X-Bitbeard-Account-Id"


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


async def _run_reconciler(ledger: QuotaLedger, interval: float) -> None:
    """Run reconciliation on an interval; a failing tick is logged, not fatal."""
    while True:
        await asyncio.sleep(interval)
        try:
            # Blocking store + disk work stays out of the event loop.
            await asyncio.to_thread(ledger.reconcile)
        except Exception:
            logger.exception("Storage Quota reconciliation failed.")


def create_app(
    config: Config | None = None,
    qbt_client: Any | None = None,
    announcer: Any | None = None,
) -> FastAPI:
    """Build the manager app. Dependencies are injectable for tests."""
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The manager reaches the same store the API owns; making sure the
        # file and schema exist (never seeding accounts — that is the API's
        # job) keeps submissions working even when the manager starts first.
        db.initialize(config.db_path)
        task = None
        if config.reconcile_interval > 0:
            task = asyncio.create_task(
                _run_reconciler(app.state.quota_ledger, config.reconcile_interval)
            )
            app.state.reconciler_task = task
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Bitbeard Download Manager", lifespan=lifespan)
    app.state.config = config
    app.state.qbt_client = qbt_client or make_qbt_client(config)
    app.state.quota_ledger = QuotaLedger(
        config.db_path, config.base_download_dir, qbt_client=app.state.qbt_client
    )
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
        x_bitbeard_account_id: int | None = Header(default=None),
    ):
        """Submit a Download and open its ownership record.

        The trusted headers name the owner: a linked Account by id (the
        row's ``account`` and the counter that is charged), or the
        ``discord:<name>`` Actor when the submitter is unlinked. The
        Storage Quota pre-check itself belongs to the API, which owns
        identity and the cap configuration; by the time a submission
        reaches the manager it has already been allowed.
        """
        actor = x_bitbeard_account or x_bitbeard_discord
        record = app.state.quota_ledger.open_download(
            account_id=x_bitbeard_account_id,
            discord_name=x_bitbeard_discord,
            title=request.title,
            category=request.category,
            size_bytes=request.size_bytes,
        )
        app.state.download_manager.submit(
            request.magnet_link,
            request.category,
            actor=actor,
            record=record,
        )
        return {
            "message": "Download started successfully",
            "download_id": record.id,
        }

    @app.get("/progress", dependencies=service_auth)
    async def api_progress():
        return {"progress": app.state.download_manager.progress()}

    @app.post("/cancel", dependencies=service_auth)
    async def api_cancel():
        return {"cancelled": app.state.download_manager.cancel_all()}

    @app.post("/media/{download_id}/delete", dependencies=service_auth)
    async def api_media_delete(download_id: int):
        """Hard-delete a finished Download: files, record, quota.

        This is the manager-side operation ticket #6's user-facing delete
        will call; a still-Active Torrent's paths are Protected and a
        sandbox-breaking path is refused.
        """
        try:
            deleted = app.state.quota_ledger.delete_download(download_id)
        except ActiveTorrentError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OutsideMediaRootError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if deleted is None:
            raise HTTPException(status_code=404, detail="No such Download.")
        return {"deleted": deleted["id"], "name": deleted["name"]}

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=9871)
