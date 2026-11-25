"""The Bitbeard Download Manager service.

Owns the torrent lifecycle, the qBittorrent client, search (Jackett/Torznab),
and all file operations on the Media Root. The API and Bot reach it with the
shared ``SERVICE_TOKEN``.

Ownership and Storage Quota live here too (the manager owns the
``downloads`` records and the per-Account usage counter): every submission
opens a record, the torrent lifecycle keeps it current, a periodic
reconciliation job recomputes the counters from the records vs on-disk
state, and the ``/media`` endpoints are the user-facing My Media operations
of ticket #6 — list, delete (files, record, quota), rename, move to another
Category, and a Download-level cancel — each scoped to the trusted Actor
the API names and guarded against Active Torrents and Media Root escapes.
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

import qbittorrentapi
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from shared import db
from shared.service_auth import require_service_token

from .config import Config, load_config
from .downloads import DownloadManager
from .events import WebhookAnnouncer
from .files import (
    FileManager,
    InvalidPathError,
    ProtectedPathError,
)
from .quota import (
    ActiveTorrentError,
    NotCompletedError,
    OutsideMediaRootError,
    PathTakenError,
    QuotaLedger,
)
from .search import search
from .stall_watchdog import StallWatchdog

logger = logging.getLogger(__name__)

#: The Categories a Download can live under, the subdirectories of the
#: Media Root a move can target.
CATEGORIES = ("Movies", "Shows", "Audiobooks", "eBooks", "Music", "Other")


class TorrentRequest(BaseModel):
    magnet_link: str
    category: str
    #: The projected size from the Torznab result, when the submitter had
    #: one; 0 means unknown (the record self-corrects at completion).
    size_bytes: int = 0
    #: The Torznab title, when known; qBittorrent's real name replaces it.
    title: str | None = None


class MediaRenameRequest(BaseModel):
    #: The Download's new name — also the directory's new leaf.
    name: str


class MediaMoveRequest(BaseModel):
    #: The Category to move the Download into.
    category: str


class FileDeleteRequest(BaseModel):
    #: The path to delete, relative to the Media Root ("/"-separated).
    path: str = Field(min_length=1)


class FileRenameRequest(BaseModel):
    #: The path to rename, relative to the Media Root.
    path: str = Field(min_length=1)
    #: The new leaf name (no separators — this is a rename, not a move).
    new_name: str = Field(min_length=1)


class FileMoveRequest(BaseModel):
    #: The path to move, relative to the Media Root.
    path: str = Field(min_length=1)
    #: The destination directory, relative to the Media Root.
    target_dir: str = Field(min_length=1)


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


def validate_download_name(name: str) -> str:
    """Reject names that could not name a directory on the Media Root."""
    cleaned = (name or "").strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or any(bad in cleaned for bad in ("/", "\\", "\x00"))
    ):
        raise HTTPException(
            status_code=400,
            # USER-FACING
            detail="That name cannot be used for a download.",
        )
    return cleaned


async def _run_reconciler(ledger: QuotaLedger, interval: float) -> None:
    """Run reconciliation on an interval; a failing tick is logged, not fatal."""
    while True:
        await asyncio.sleep(interval)
        try:
            # Blocking store + disk work stays out of the event loop.
            await asyncio.to_thread(ledger.reconcile)
        except Exception:
            logger.exception("Storage Quota reconciliation failed.")


async def _run_stall_watchdog(watchdog: StallWatchdog, interval: float) -> None:
    """Run stall sweeps on an interval; a failing sweep is logged, not fatal."""
    while True:
        await asyncio.sleep(interval)
        try:
            # Blocking qBittorrent API calls stay out of the event loop.
            await asyncio.to_thread(watchdog.check_once)
        except Exception:
            logger.exception("Stall watchdog sweep failed.")


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
        reconciler = None
        if config.reconcile_interval > 0:
            reconciler = asyncio.create_task(
                _run_reconciler(app.state.quota_ledger, config.reconcile_interval)
            )
            app.state.reconciler_task = reconciler
        stall_task = None
        if config.stall_watch_interval > 0:
            watchdog = StallWatchdog(
                app.state.qbt_client,
                port_rotate_polls=config.stall_port_rotate_polls,
            )
            app.state.stall_watchdog = watchdog
            stall_task = asyncio.create_task(
                _run_stall_watchdog(watchdog, config.stall_watch_interval)
            )
            app.state.stall_watchdog_task = stall_task
        yield
        for task in (reconciler, stall_task):
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
    app.state.file_manager = FileManager(
        config.db_path,
        config.base_download_dir,
        qbt_client=app.state.qbt_client,
    )

    service_auth = [Depends(require_service_token(config.service_token))]

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "manager"}

    @app.get("/search", dependencies=service_auth)
    async def api_search(q: str = Query(...)):
        # search() does blocking network I/O; keep it off the event loop so a
        # slow Jackett/indexer can't freeze the whole service.
        results = await asyncio.to_thread(search, q, config=config)
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
            # USER-FACING
            "message": "Download started successfully",
            "download_id": record.id,
        }

    @app.get("/progress", dependencies=service_auth)
    async def api_progress():
        return {"progress": app.state.download_manager.progress()}

    @app.post("/cancel", dependencies=service_auth)
    async def api_cancel():
        return {"cancelled": app.state.download_manager.cancel_all()}

    # --- My Media (ticket #6): Download-level user operations -------------
    #
    # The trusted headers name the Actor (ADR-0002); ownership is enforced
    # here as defense in depth behind the API's boundary check, and fails
    # closed: a call that names no Actor operates on nothing.

    def require_owned_download(
        download_id: int,
        *,
        account_id: int | None,
        discord_name: str | None,
    ):
        """Resolve the Download a media operation targets, and make sure
        the trusted Actor owns it.

        Users operate only on their own Downloads. There is no admin
        passthrough on these user endpoints — an Admin manages other
        Actors' media through the Admin Panel's file manager.
        """
        row = db.get_download(config.db_path, download_id)
        if row is None:
            # USER-FACING
            raise HTTPException(status_code=404, detail="No such download.")
        if account_id is None and discord_name is None:
            raise HTTPException(
                # USER-FACING
                status_code=403, detail="No trusted actor on the request."
            )
        if account_id is not None:
            owner_mismatch = row["account"] != account_id
        else:
            owner_mismatch = row["discord_name"] != discord_name
        if owner_mismatch:
            raise HTTPException(
                status_code=403,
                # USER-FACING
                detail="This download belongs to another Actor.",
            )
        return row

    @contextlib.contextmanager
    def media_guards():
        """Map the ledger's media refusals onto user-facing statuses."""
        try:
            yield
        except ActiveTorrentError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except NotCompletedError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OutsideMediaRootError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except PathTakenError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/media", dependencies=service_auth)
    async def api_media_list(
        x_bitbeard_account_id: int | None = Header(default=None),
        x_bitbeard_discord: str | None = Header(default=None),
    ):
        """One Actor's Downloads for the My Media page.

        In-flight rows come back with live progress; completed rows are
        the ones the Account can delete, rename and move.
        """
        if x_bitbeard_account_id is None and x_bitbeard_discord is None:
            raise HTTPException(
                status_code=403, detail="No trusted actor on the request."
            )
        downloads = app.state.quota_ledger.list_downloads(
            account_id=x_bitbeard_account_id,
            discord_name=x_bitbeard_discord,
        )
        return {"downloads": downloads}

    @app.post("/media/{download_id}/delete", dependencies=service_auth)
    async def api_media_delete(
        download_id: int,
        x_bitbeard_account_id: int | None = Header(default=None),
        x_bitbeard_discord: str | None = Header(default=None),
    ):
        """Hard-delete the Actor's finished Download: files, record, quota.

        A still-Active Torrent's paths are Protected (409 — the user
        cancels instead) and a sandbox-breaking path is refused (400).
        """
        require_owned_download(
            download_id,
            account_id=x_bitbeard_account_id,
            discord_name=x_bitbeard_discord,
        )
        with media_guards():
            deleted = app.state.quota_ledger.delete_download(download_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail="No such download.")
        return {"deleted": deleted["id"], "name": deleted["name"]}

    @app.post("/media/{download_id}/rename", dependencies=service_auth)
    async def api_media_rename(
        download_id: int,
        request: MediaRenameRequest,
        x_bitbeard_account_id: int | None = Header(default=None),
        x_bitbeard_discord: str | None = Header(default=None),
    ):
        """Rename the Actor's completed Download: record + disk directory."""
        name = validate_download_name(request.name)
        require_owned_download(
            download_id,
            account_id=x_bitbeard_account_id,
            discord_name=x_bitbeard_discord,
        )
        with media_guards():
            updated = app.state.quota_ledger.rename_download(download_id, name)
        if updated is None:
            raise HTTPException(status_code=404, detail="No such download.")
        return {"renamed": updated["id"], "name": updated["name"]}

    @app.post("/media/{download_id}/move", dependencies=service_auth)
    async def api_media_move(
        download_id: int,
        request: MediaMoveRequest,
        x_bitbeard_account_id: int | None = Header(default=None),
        x_bitbeard_discord: str | None = Header(default=None),
    ):
        """Move the Actor's completed Download to another Category.

        Record and disk follow; the Storage Quota is untouched.
        """
        if request.category not in CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=(
                    # USER-FACING
                    f"Unknown Category '{request.category}'. Choose one of: "
                    f"{', '.join(CATEGORIES)}."
                ),
            )
        require_owned_download(
            download_id,
            account_id=x_bitbeard_account_id,
            discord_name=x_bitbeard_discord,
        )
        with media_guards():
            updated = app.state.quota_ledger.move_download(
                download_id, request.category
            )
        if updated is None:
            raise HTTPException(status_code=404, detail="No such download.")
        return {
            "moved": updated["id"],
            "category": updated["category"],
            "name": updated["name"],
        }

    @app.post("/media/{download_id}/cancel", dependencies=service_auth)
    async def api_media_cancel(
        download_id: int,
        x_bitbeard_account_id: int | None = Header(default=None),
        x_bitbeard_discord: str | None = Header(default=None),
    ):
        """Cancel one In-flight Download: only its own torrent is swept.

        Unlike the panel-level ``POST /cancel``, this is Download-level:
        the torrent parked at the record's save path is matched by path,
        deleted from qBittorrent, and the record is settled as cancelled
        (quota freed).
        """
        row = require_owned_download(
            download_id,
            account_id=x_bitbeard_account_id,
            discord_name=x_bitbeard_discord,
        )
        cancelled = app.state.download_manager.cancel_for(row["save_path"])
        if not cancelled:
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail="That download is not in progress anymore.",
            )
        app.state.quota_ledger.cancel(download_id)
        return {"cancelled": cancelled}

    # --- Admin file manager (ticket #10) ------------------------------------
    #
    # The API gates these on the Admin Role and audits them; the manager
    # owns the media volume, so the sandbox, the Protected Path guards and
    # the Download-record bookkeeping all live in manager.files.

    @contextlib.contextmanager
    def file_guards():
        """Map the file manager's refusals onto Admin Panel statuses."""
        try:
            yield
        except ProtectedPathError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (OutsideMediaRootError, InvalidPathError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except PathTakenError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/files", dependencies=service_auth)
    async def api_files(path: str = Query(default="")):
        """One directory of the Media Root tree, for the panel's browser.

        ``path`` is relative to the Media Root ("" is the root itself); a
        path that does not exist — or that escapes the Media Root — is
        refused.
        """
        with file_guards():
            return app.state.file_manager.browse(path)

    @app.post("/files/delete", dependencies=service_auth)
    async def api_files_delete(request: FileDeleteRequest):
        """Hard-delete a file or directory tree under the Media Root.

        Protected Paths (Active Torrents) are refused with 409; a
        whole Download's save path removal drops its record and frees the
        owner's Storage Quota.
        """
        with file_guards():
            return app.state.file_manager.delete(request.path)

    @app.post("/files/rename", dependencies=service_auth)
    async def api_files_rename(request: FileRenameRequest):
        """Rename a file or directory, keeping its place in the tree."""
        with file_guards():
            return app.state.file_manager.rename(
                request.path, request.new_name
            )

    @app.post("/files/move", dependencies=service_auth)
    async def api_files_move(request: FileMoveRequest):
        """Move a file or directory under another directory of the tree."""
        with file_guards():
            return app.state.file_manager.move(
                request.path, request.target_dir
            )

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=9871)
