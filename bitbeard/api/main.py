"""The Bitbeard API application.

The API is the only service a Frontend talks to. It owns identity, Sessions and
the Audit Ledger, and holds the docker socket for Admin Panel subsystem
control. It does not talk to qBittorrent or Jackett; media work belongs to the
Download Manager.

Authentication lives here at the boundary (ADR-0002): everything except
``/health``, the ``/auth`` endpoints and the token-authed ``/internal/events``
requires a valid Session cookie, and admin endpoints additionally require the
Admin Role. Frontend-service callers (the Bot) take the same user endpoints
with the shared SERVICE_TOKEN plus the acting Discord user's headers — the
API resolves a linked Account for attribution, or records the Actor as
``discord:<name>`` when the Discord user is unlinked. User-facing media work
(Search, Download submission, progress, cancel, and the My Media
operations on the Actor's own Downloads) is proxied to the Download
Manager with the shared SERVICE_TOKEN and the acting Account as a trusted
header — the API never touches qBittorrent or Jackett itself.

The API also owns the Storage Quota decision (it owns identity and the cap
configuration): every Download submission is pre-checked against the
Actor's projected usage (on-disk plus In-flight) before it is proxied, and
Admins may set per-Account caps (override / unlimited / global default).
The manager maintains the usage counter those checks read.
"""

import logging
import posixpath
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from shared import db
from shared.security import verify_password
from shared.service_auth import require_service_token

from .config import Config, load_config
from .docker_control import (
    CONTAINER_ACTIONS,
    DockerControl,
    DockerUnavailable,
    OPTIONAL_SUBSYSTEMS,
    subsystem_ports,
)
from .manager_proxy import ManagerProxy, ManagerUnavailable, Transport
from .sessions import SessionStore
from .vpn import VpnControl, VpnSettings

logger = logging.getLogger(__name__)

#: The HTTP-only cookie that carries the Session token.
COOKIE_NAME = "bitbeard_session"

#: Trusted headers (ADR-0002): the acting Discord user on a bot-proxied
#: request — the ID to resolve against linked Accounts, and the display
#: name used for ``discord:<name>`` attribution when it is unlinked.
DISCORD_ID_HEADER = "X-Bitbeard-Discord"
DISCORD_NAME_HEADER = "X-Bitbeard-Discord-Name"

#: Actor recorded for Lifecycle Events that carry no acting Account.
MANAGER_ACTOR = "download-manager"


@dataclass(frozen=True)
class Caller:
    """The Actor behind a request, resolved at the API boundary.

    A Session caller is the Account itself. A Frontend-service caller (the
    Bot acting for a Discord user) resolves to the linked Account when
    there is one, or becomes a synthetic User-role Actor named
    ``discord:<name>`` when the Discord user is unlinked.
    """

    token: str
    account_id: int | None
    username: str
    role: str
    discord_id: str | None = None
    #: How the Caller arrived: "session" (cookie) or "discord" (the Bot).
    via: str = "session"


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class DownloadRequest(BaseModel):
    magnet_link: str
    category: str
    #: The projected size from the Torznab result, when the submitter had
    #: one; 0 means unknown (the record self-corrects at completion).
    size_bytes: int = Field(default=0, ge=0)
    #: The Torznab title, when known; qBittorrent's real name replaces it.
    title: str | None = None


class MediaRenameRequest(BaseModel):
    #: The Download's new name — the manager renames record and directory.
    name: str = Field(min_length=1)


class MediaMoveRequest(BaseModel):
    #: The Category to move the Download into.
    category: str = Field(min_length=1)


class LinkCodeRequest(BaseModel):
    #: The Discord ID the Account claims. The binding is not made here —
    #: it is verified when the Bot next relays this Discord user's ID.
    discord_id: str = Field(min_length=1)


class LifecycleEvent(BaseModel):
    """One Lifecycle Event as the Download Manager emits it."""

    event: Literal["submitted", "completed", "drained"]
    torrent_name: str | None = None
    category: str | None = None
    actor: str | None = None
    #: The Download's ownership-record id, so ledger rows can be traced.
    download_id: int | None = None


class QuotaAssignment(BaseModel):
    """A per-Account Storage Quota override.

    ``None`` resets the Account to the global default; the
    ``QUOTA_UNLIMITED`` sentinel removes its cap; any other value is the
    cap in bytes.
    """

    quota_bytes: int | None = Field(default=None, ge=db.QUOTA_UNLIMITED)


class AccountCreation(BaseModel):
    """A new Account as the Admin Panel submits it.

    The Role is mandatory — there is no sensible default to hide behind.
    The Storage Quota follows the same encoding as ``QuotaAssignment``:
    ``None`` rides the global default, the ``QUOTA_UNLIMITED`` sentinel
    removes the cap, any other value is the cap in bytes.
    """

    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: Literal["admin", "user"]
    quota_bytes: int | None = Field(default=None, ge=db.QUOTA_UNLIMITED)


class PasswordReset(BaseModel):
    """The new password an Admin sets on an Account (no email in this
    stack, so the panel hands it to the Admin out of band)."""

    new_password: str = Field(min_length=8)


class FileDeleteRequest(BaseModel):
    """One file-manager delete, as the Admin Panel submits it.

    The path is relative to the Media Root — the shape the manager's
    browser navigates with; the manager owns the sandbox and refuses
    escapes and Protected Paths.
    """

    path: str = Field(min_length=1)


class FileRenameRequest(BaseModel):
    """One file-manager rename: a path plus its new leaf name."""

    path: str = Field(min_length=1)
    new_name: str = Field(min_length=1)


class FileMoveRequest(BaseModel):
    """One file-manager move: a path plus the destination directory."""

    path: str = Field(min_length=1)
    target_dir: str = Field(min_length=1)


class VpnConfigRequest(BaseModel):
    """A VPN settings edit from the Admin Panel's VPN page.

    Every field is optional: ``None`` leaves the stored value untouched, so
    an edit that only changes the provider does not clobber the
    credentials. The password is written to the settings file but never
    returned to a Frontend or written to the ledger.
    """

    provider: str | None = None
    openvpn_user: str | None = None
    openvpn_password: str | None = None
    server: str | None = None


def format_size(size_bytes: int) -> str:
    """One human-readable size for quota messages."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def quota_view(account, config: Config) -> dict:
    """Shape an Account's Storage Quota state for the Frontends.

    ``cap_bytes`` is the effective cap — the Account's override, else the
    global default — and ``None`` when the Account is exempt (Admins) or
    unlimited. ``used_bytes`` is the projected usage the pre-check
    compares against the cap (completed Downloads' on-disk sizes plus
    In-flight sizes), read straight from the records so display and
    enforcement can never disagree.
    """
    override = account["quota_bytes"]
    used = db.sum_download_sizes(config.db_path, account["id"])
    if account["role"] == "admin":
        return {
            "used_bytes": used,
            "cap_bytes": None,
            "unlimited": False,
            "exempt": True,
            "override_bytes": override,
        }
    unlimited = override == db.QUOTA_UNLIMITED
    cap = None if unlimited else (
        override if override is not None else config.default_quota_bytes
    )
    return {
        "used_bytes": used,
        "cap_bytes": cap,
        "unlimited": unlimited,
        "exempt": False,
        "override_bytes": override,
    }


def create_app(
    config: Config | None = None,
    proxy_transport: Transport | None = None,
    docker_control: DockerControl | None = None,
    vpn_control: VpnControl | None = None,
) -> FastAPI:
    """Build the API app. ``config``, the manager transport, the docker
    control and the VPN control are injectable for tests."""
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        seeded = db.initialize(
            config.db_path,
            admin_user=config.admin_user,
            admin_password=config.admin_password,
        )
        if seeded:
            logger.info(
                "Seeded bootstrap Admin '%s' from env.", config.admin_user
            )
        else:
            logger.info("User store already initialised at %s.", config.db_path)
        yield

    app = FastAPI(title="Bitbeard API", lifespan=lifespan)
    app.state.config = config
    app.state.sessions = SessionStore(
        config.db_path, ttl_hours=config.session_ttl_hours
    )

    def require_session(
        session_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ) -> Caller:
        """Resolve the calling Account from the Session cookie, or refuse.

        This dependency is the API boundary: any route that takes it refuses
        requests without a live Session, a deleted Account, or a disabled
        Account. Tests may replace ``app.state.sessions`` (e.g. with a fake
        clock) before the first request.
        """
        if not session_token:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        session = app.state.sessions.resolve(session_token)
        if session is None:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        account = db.get_account(config.db_path, session.account_id)
        if account is None or account["disabled"]:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        return Caller(
            token=session.token,
            account_id=account["id"],
            username=account["username"],
            role=account["role"],
            discord_id=account["discord_id"],
        )

    def require_caller(
        session_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
        authorization: str | None = Header(default=None),
        discord_id: str | None = Header(default=None, alias=DISCORD_ID_HEADER),
        discord_name: str | None = Header(
            default=None, alias=DISCORD_NAME_HEADER
        ),
    ) -> Caller:
        """Resolve the calling Actor from either Frontend authentication.

        Browsers arrive with a Session cookie; the Bot arrives with the
        shared SERVICE_TOKEN plus the acting Discord user's ID (and, when
        it has one, display name) in the trusted headers. A linked Discord
        ID resolves to that Account — including its Role; an unlinked one
        becomes a synthetic ``discord:<name>`` Actor that can use the
        Webui-level endpoints but never the Admin Panel.
        """
        if authorization is not None:
            if not secrets.compare_digest(
                authorization, f"Bearer {config.service_token}"
            ):
                raise HTTPException(status_code=401, detail="Not authenticated.")
            if not discord_id:
                raise HTTPException(
                    status_code=401,
                    detail="The acting Discord user must ride along.",
                )
            # Link Code verification (ADR-0002): a pending code claiming
            # this Discord ID is spent by the interaction itself — the
            # "next bot interaction" that proves the user controls the
            # ID. From here on the normal resolution sees the freshly
            # linked Account.
            linked = db.consume_pending_link(config.db_path, discord_id)
            if linked is not None:
                db.record_audit(
                    config.db_path,
                    actor=linked["username"],
                    action="discord_linked",
                    details={"discord_id": discord_id},
                )
                logger.info(
                    "Link Code verified: Account '%s' bound to Discord %s.",
                    linked["username"],
                    discord_id,
                )
            account = db.find_account_by_discord(config.db_path, discord_id)
            if account is not None:
                if account["disabled"]:
                    raise HTTPException(
                        status_code=401, detail="Not authenticated."
                    )
                return Caller(
                    token="",
                    account_id=account["id"],
                    username=account["username"],
                    role=account["role"],
                    discord_id=account["discord_id"],
                    via="discord",
                )
            name = discord_name or discord_id
            return Caller(
                token="",
                account_id=None,
                username=f"discord:{name}",
                role="user",
                discord_id=discord_id,
                via="discord",
            )
        return require_session(session_token)

    def require_admin(caller: Caller = Depends(require_session)) -> Caller:
        """Refuse non-Admin Accounts at the boundary."""
        if caller.role != "admin":
            raise HTTPException(status_code=403, detail="Admin Role required.")
        return caller

    proxy = ManagerProxy(config.manager_url, config.service_token, proxy_transport)
    app.state.manager_proxy = proxy

    control = docker_control or DockerControl(config.compose_project)
    app.state.docker_control = control

    vpn = vpn_control or VpnControl(
        config.openvpn_dir, provider_fallback=config.vpn_provider
    )
    app.state.vpn_control = vpn

    def docker_or_502(action):
        """Run one docker action or report the socket as broken.

        Like the manager hop, docker being unavailable is not the Admin
        Panel's concern to diagnose: every failure on the API<->docker hop
        becomes one 502 the panel can show inline.
        """
        try:
            return action()
        except DockerUnavailable as exc:
            logger.warning("Docker unavailable: %s", exc)
            raise HTTPException(
                status_code=502, detail="Docker is unavailable."
            )

    def service_view(name: str, statuses: dict[str, str], ports: dict) -> dict:
        """One Subsystem's row for the panel: state, probe, disable flag."""
        return {
            "name": name,
            "state": statuses.get(name, "absent"),
            "port": ports[name],
            "probe": control.probe(name),
            "optional": name in OPTIONAL_SUBSYSTEMS,
            "disabled": db.service_disabled(config.db_path, name),
        }

    def forward_or_502(method: str, path: str, **kwargs) -> dict:
        """Reach the manager or report the hop as broken.

        The manager's availability is not the Frontends' concern: any
        failure on the API<->manager hop becomes one 502 the Webui can
        show inline.
        """
        try:
            response = proxy.forward(method, path, **kwargs)
        except ManagerUnavailable as exc:
            logger.warning("Download Manager unavailable: %s", exc)
            raise HTTPException(
                status_code=502, detail="Download Manager unavailable."
            )
        if response.status_code >= 400:
            logger.warning(
                "Download Manager refused a proxied call with %s.",
                response.status_code,
            )
            raise HTTPException(
                status_code=502, detail="Download Manager unavailable."
            )
        return response.payload

    def actor_forward(caller: Caller) -> dict:
        """Trusted-header identity for the manager hop (ADR-0002).

        A Session caller forwards its Account name and linked Discord ID.
        The Bot acting for a linked Account forwards that Account; for an
        unlinked Discord user there is no Account, so the lifecycle events
        the manager emits are attributed to the same ``discord:<name>``
        Actor the Audit Ledger records — and the Download's ownership
        record carries that Actor instead of an Account id.
        """
        if caller.via == "discord" and caller.account_id is None:
            return {
                "account": None,
                "discord": caller.username,
                "account_id": None,
            }
        return {
            "account": caller.username,
            "discord": caller.discord_id,
            "account_id": caller.account_id,
        }

    def require_owned_download(caller: Caller, download_id: int) -> dict:
        """The Download record the Actor targets, or the boundary refusal.

        Role and ownership are enforced here, where identity lives:
        users operate only on their own Downloads, and an unknown record
        is refused without ever reaching the manager. There is no admin
        passthrough — the Admin Panel's file manager is how an Admin
        touches another Actor's media.
        """
        row = db.get_download(config.db_path, download_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such Download.")
        if caller.account_id is not None:
            owner_mismatch = row["account"] != caller.account_id
        else:
            owner_mismatch = row["discord_name"] != caller.username
        if owner_mismatch:
            raise HTTPException(
                status_code=403,
                detail="This Download belongs to another Actor.",
            )
        return row

    def forward_media_or_502(method: str, path: str, **kwargs) -> dict:
        """Reach the manager for a media operation, passing refusals through.

        The manager owns the Protected Path and Media Root guards, so its
        deliberate 4xx answers (unknown Download, not owned, Active
        Torrent, bad path or Category) surface to the Frontend verbatim —
        the Webui renders them inline. Only a broken hop or a crashing
        manager becomes the one 502.
        """
        try:
            response = proxy.forward(method, path, **kwargs)
        except ManagerUnavailable as exc:
            logger.warning("Download Manager unavailable: %s", exc)
            raise HTTPException(
                status_code=502, detail="Download Manager unavailable."
            )
        if response.status_code >= 500:
            logger.warning(
                "Download Manager failed a proxied call with %s.",
                response.status_code,
            )
            raise HTTPException(
                status_code=502, detail="Download Manager unavailable."
            )
        if response.status_code >= 400:
            detail = None
            if isinstance(response.payload, dict):
                detail = response.payload.get("detail")
            raise HTTPException(
                status_code=response.status_code,
                detail=detail or "Download Manager refused the request.",
            )
        return response.payload

    def enforce_quota(caller: Caller, size_bytes: int) -> None:
        """Refuse a submission that would exceed the Actor's Storage Quota.

        Projected usage is what the Account's ``downloads`` records already
        account for (completed Downloads' on-disk sizes plus In-flight
        sizes) plus the new Download's size. The cap is the Account's
        override, the unlimited sentinel, or the global default; Admins
        are exempt, as are unlinked Discord users, who have no Account to
        charge. The error is the message the Webui shows inline and the
        Bot relays to the Discord channel.
        """
        if caller.role == "admin" or caller.account_id is None:
            return
        account = db.get_account(config.db_path, caller.account_id)
        if account is None:
            return
        override = account["quota_bytes"]
        if override == db.QUOTA_UNLIMITED:
            return
        cap = override if override is not None else config.default_quota_bytes
        projected = (
            db.sum_download_sizes(config.db_path, caller.account_id) + size_bytes
        )
        if projected > cap:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Quota exceeded: this Download would put you at "
                    f"{format_size(projected)} of your {format_size(cap)} "
                    "limit. Delete something first."
                ),
            )

    @app.get("/search")
    async def search(q: str = Query(...), caller: Caller = Depends(require_caller)):
        """Search all Indexers, through the manager, as this Actor.

        The query and the Actor are written to the Audit Ledger here, once,
        at the boundary: the manager neither audits nor learns who to
        attribute the Search to beyond the trusted header.
        """
        payload = forward_or_502(
            "GET", "/search", params={"q": q}, **actor_forward(caller)
        )
        details = {"query": q}
        if caller.via == "discord":
            details["via"] = "discord"
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="search",
            details=details,
        )
        return payload

    @app.post("/download")
    async def download(
        request: DownloadRequest, caller: Caller = Depends(require_caller)
    ):
        """Submit a Download through the manager on behalf of this Actor.

        The Storage Quota pre-check runs here, before the proxy: the API
        owns identity and the cap configuration, so an over-cap submission
        is rejected without ever reaching the manager.
        """
        enforce_quota(caller, request.size_bytes)
        payload = forward_or_502(
            "POST",
            "/download",
            json={
                "magnet_link": request.magnet_link,
                "category": request.category,
                "size_bytes": request.size_bytes,
                "title": request.title,
            },
            **actor_forward(caller),
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="download_submit",
            details={
                "category": request.category,
                "via": "discord" if caller.via == "discord" else "webui",
            },
        )
        return payload

    @app.get("/progress")
    async def progress(caller: Caller = Depends(require_caller)):
        """In-flight Download progress, as the manager sees it."""
        return forward_or_502("GET", "/progress")

    @app.post("/cancel")
    async def cancel(caller: Caller = Depends(require_caller)):
        """Cancel Downloads through the manager and record the Actor."""
        payload = forward_or_502(
            "POST", "/cancel", **actor_forward(caller)
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="download_cancel",
            details=payload,
        )
        return payload

    @app.get("/media")
    async def my_media(caller: Caller = Depends(require_caller)):
        """The calling Actor's Downloads, for the Webui's My Media page.

        Listing is proxied: the manager owns the records and the live
        torrent progress, and scopes the answer to the trusted Actor the
        API names — a user sees only their own Downloads.
        """
        return forward_media_or_502("GET", "/media", **actor_forward(caller))

    @app.post("/media/{download_id}/delete")
    async def media_delete(
        download_id: int, caller: Caller = Depends(require_caller)
    ):
        """Delete the Actor's completed Download through the manager.

        Ownership is checked here at the boundary; the manager does the
        file work and refuses Protected Paths (Active Torrents) and
        Media Root escapes. Deleting frees quota, so this is an audited
        file operation.
        """
        row = require_owned_download(caller, download_id)
        payload = forward_media_or_502(
            "POST", f"/media/{download_id}/delete", **actor_forward(caller)
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="media_delete",
            details={
                "download_id": download_id,
                "name": row["name"],
                "save_path": row["save_path"],
            },
        )
        return payload

    @app.post("/media/{download_id}/rename")
    async def media_rename(
        download_id: int,
        request: MediaRenameRequest,
        caller: Caller = Depends(require_caller),
    ):
        """Rename the Actor's completed Download: record + disk directory."""
        row = require_owned_download(caller, download_id)
        payload = forward_media_or_502(
            "POST",
            f"/media/{download_id}/rename",
            json={"name": request.name},
            **actor_forward(caller),
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="media_rename",
            details={
                "download_id": download_id,
                "name": row["name"],
                "new_name": request.name,
            },
        )
        return payload

    @app.post("/media/{download_id}/move")
    async def media_move(
        download_id: int,
        request: MediaMoveRequest,
        caller: Caller = Depends(require_caller),
    ):
        """Move the Actor's completed Download to another Category.

        Quota is unchanged — the manager moves the same bytes to a new
        Category directory and repoints the record.
        """
        row = require_owned_download(caller, download_id)
        payload = forward_media_or_502(
            "POST",
            f"/media/{download_id}/move",
            json={"category": request.category},
            **actor_forward(caller),
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="media_move",
            details={
                "download_id": download_id,
                "name": row["name"],
                "category": request.category,
                "save_path": row["save_path"],
            },
        )
        return payload

    @app.post("/media/{download_id}/cancel")
    async def media_cancel(
        download_id: int, caller: Caller = Depends(require_caller)
    ):
        """Cancel one In-flight Download: the Actor's own torrent only.

        Unlike the panel-level ``POST /cancel``, this is Download-level —
        the manager sweeps only the torrent parked at this record's save
        path and settles the record as cancelled.
        """
        require_owned_download(caller, download_id)
        payload = forward_media_or_502(
            "POST", f"/media/{download_id}/cancel", **actor_forward(caller)
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="download_cancel",
            details={"download_id": download_id, **payload},
        )
        return payload

    @app.post(
        "/internal/events",
        dependencies=[Depends(require_service_token(config.service_token))],
    )
    async def internal_events(event: LifecycleEvent):
        """Record a Lifecycle Event the manager emitted (ADR-0002).

        This is the token-authed back door the Session gate deliberately
        does not cover: the caller is the manager, not a browser.
        """
        db.record_audit(
            config.db_path,
            actor=event.actor or MANAGER_ACTOR,
            action=f"lifecycle_{event.event}",
            details={
                "torrent_name": event.torrent_name,
                "category": event.category,
                "download_id": event.download_id,
            },
        )
        return {"received": True}

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "api"}

    @app.post("/auth/login")
    async def login(request: LoginRequest, response: Response):
        """Check credentials, issue a Session, and report the Account's Role.

        Unknown usernames and wrong passwords get the same response so the
        endpoint does not disclose which usernames exist.
        """
        account = db.find_account(config.db_path, request.username)
        if account is None or not verify_password(
            request.password, account["password_hash"]
        ):
            db.record_audit(
                config.db_path,
                actor=request.username,
                action="login_failed",
                details={"reason": "invalid credentials"},
            )
            raise HTTPException(
                status_code=401, detail="Invalid username or password."
            )
        if account["disabled"]:
            db.record_audit(
                config.db_path,
                actor=account["username"],
                action="login_failed",
                details={"reason": "account disabled"},
            )
            raise HTTPException(status_code=403, detail="Account is disabled.")

        session = app.state.sessions.issue(account["id"])
        response.set_cookie(
            COOKIE_NAME,
            session.token,
            max_age=int(app.state.sessions.ttl.total_seconds()),
            httponly=True,
            samesite="lax",
            path="/",
        )
        db.record_audit(
            config.db_path, actor=account["username"], action="login", details={}
        )
        return {"role": account["role"]}

    @app.get("/auth/me")
    async def me(caller: Caller = Depends(require_session)):
        """Echo the calling Account, so Frontends can restore their view."""
        return {"username": caller.username, "role": caller.role}

    @app.get("/me/quota")
    async def me_quota(caller: Caller = Depends(require_session)):
        """The calling Account's Storage Quota usage vs cap, for the Webui.

        The search page renders this inline; ``cap_bytes`` is ``None`` for
        Admins (exempt) and unlimited Accounts, so the Frontend can show a
        "no cap" state instead of a false zero.
        """
        account = db.get_account(config.db_path, caller.account_id)
        if account is None:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        return quota_view(account, config)

    @app.post("/auth/logout")
    async def logout(response: Response, caller: Caller = Depends(require_session)):
        """Revoke the calling Session server-side and clear the cookie."""
        app.state.sessions.revoke(caller.token)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"message": "Logged out."}

    @app.post("/auth/password")
    async def change_password(
        request: PasswordChange, caller: Caller = Depends(require_session)
    ):
        """Change the calling Account's own password.

        The current password must be re-presented, and every other Session of
        the Account is revoked so a changed password cuts off old browsers
        while keeping the browser that made the change logged in.
        """
        account = db.get_account(config.db_path, caller.account_id)
        if account is None:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        if not verify_password(request.current_password, account["password_hash"]):
            raise HTTPException(
                status_code=400, detail="Current password is incorrect."
            )
        db.update_password(config.db_path, caller.account_id, request.new_password)
        app.state.sessions.revoke_all_for_account(
            caller.account_id, keep=caller.token
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="password_changed",
            details={},
        )
        return {"message": "Password updated."}

    @app.post("/link/code")
    async def create_link_code(
        request: LinkCodeRequest, caller: Caller = Depends(require_session)
    ):
        """Issue a one-time Link Code binding this Account to a Discord ID.

        The Webui generates it and shows it once; the binding is only
        verified when the Bot next relays that Discord user's ID (the
        Frontend-service path consumes the pending code there), so a
        leaked code cannot attach someone else's Discord identity.
        Generating a new code retires the Account's previous pending one.
        Audited like every consequential action.
        """
        discord_id = request.discord_id.strip()
        if not discord_id:
            raise HTTPException(
                status_code=400, detail="A Discord ID is required."
            )
        held = db.find_account_by_discord(config.db_path, discord_id)
        if held is not None and held["id"] != caller.account_id:
            raise HTTPException(
                status_code=400,
                detail="That Discord ID is already linked to another Account.",
            )
        code = db.create_link_code(
            config.db_path, caller.account_id, discord_id
        )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="link_code_generated",
            details={"discord_id": discord_id},
        )
        return {"code": code, "discord_id": discord_id}

    @app.get("/admin/ping")
    async def admin_ping(caller: Caller = Depends(require_admin)):
        """Admin-role gate proof; real admin endpoints come with the panel."""
        return {"message": "pong", "admin": caller.username}

    @app.get("/admin/users")
    async def admin_list_users(caller: Caller = Depends(require_admin)):
        """Every Account, for the Admin Panel's Users page.

        Each Account carries its Role, disabled flag and Discord link
        state — the linked ID when bound, otherwise the live pending Link
        Code and the ID it claims — plus the Storage Quota shaped by the
        same ``quota_view`` the enforcement pre-check and the Webui read,
        so display and enforcement can never disagree. The global default
        rides along so the panel can label a "default" cap with the real
        number.
        """
        users = []
        for account in db.list_accounts(config.db_path):
            pending = db.pending_link_code(config.db_path, account["id"])
            users.append(
                {
                    "id": account["id"],
                    "username": account["username"],
                    "role": account["role"],
                    "disabled": bool(account["disabled"]),
                    "discord_id": account["discord_id"],
                    "link_code": dict(pending) if pending is not None else None,
                    "quota": quota_view(account, config),
                }
            )
        return {"users": users, "default_quota_bytes": config.default_quota_bytes}

    @app.post("/admin/users")
    async def admin_create_user(
        request: AccountCreation, caller: Caller = Depends(require_admin)
    ):
        """Create an Account with its Role and Storage Quota — audited.

        Usernames are unique: the duplicate is refused before anything is
        written, so no half-made Account lingers, and the store's UNIQUE
        constraint backs the pre-check up. The password is hashed through
        ``shared.security`` and never reaches the ledger.
        """
        username = request.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="A username is required.")
        if db.find_account(config.db_path, username) is not None:
            raise HTTPException(
                status_code=400, detail="That username is already taken."
            )
        account_id = db.create_account(
            config.db_path,
            username=username,
            password=request.password,
            role=request.role,
            quota_bytes=request.quota_bytes,
        )
        if account_id is None:
            raise HTTPException(
                status_code=400, detail="That username is already taken."
            )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="account_created",
            details={
                "account_id": account_id,
                "username": username,
                "role": request.role,
                "quota_bytes": request.quota_bytes,
            },
        )
        logger.info("Account '%s' created by %s.", username, caller.username)
        account = db.get_account(config.db_path, account_id)
        return {
            "id": account_id,
            "username": username,
            "role": request.role,
            "quota": quota_view(account, config),
        }

    @app.post("/admin/users/{account_id}/reset")
    async def admin_reset_password(
        account_id: int,
        request: PasswordReset,
        caller: Caller = Depends(require_admin),
    ):
        """Reset an Account's password — an audited Admin action.

        Every Session of the Account is revoked with it: whoever held the
        old credential must lose the browser they never logged out of.
        The new password itself never reaches the ledger.
        """
        account = db.get_account(config.db_path, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="No such Account.")
        db.update_password(config.db_path, account_id, request.new_password)
        revoked = app.state.sessions.revoke_all_for_account(account_id)
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="password_reset",
            details={
                "account_id": account_id,
                "username": account["username"],
                "sessions_revoked": revoked,
            },
        )
        return {
            "message": f"Password reset for {account['username']}.",
            "sessions_revoked": revoked,
        }

    @app.post("/admin/users/{account_id}/disable")
    async def admin_disable_user(
        account_id: int, caller: Caller = Depends(require_admin)
    ):
        """Disable an Account and revoke its Sessions — audited.

        Guard rail (the smallest sane one): the last enabled Admin cannot
        be disabled — which also covers an Admin disabling themselves when
        they are the only one. With a second enabled Admin remaining,
        self-disabling is survivable (that Admin re-enables), so it is
        allowed. Disabling already refuses every request at the boundary;
        the revocation kills live Sessions in the same motion.
        """
        account = db.get_account(config.db_path, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="No such Account.")
        if account["disabled"]:
            raise HTTPException(
                status_code=400, detail="Account is already disabled."
            )
        if (
            account["role"] == "admin"
            and db.count_enabled_admins(config.db_path) <= 1
        ):
            raise HTTPException(
                status_code=400,
                detail="Refusing to disable the last enabled Admin.",
            )
        db.set_disabled(config.db_path, account_id, True)
        revoked = app.state.sessions.revoke_all_for_account(account_id)
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="account_disabled",
            details={
                "account_id": account_id,
                "username": account["username"],
                "sessions_revoked": revoked,
            },
        )
        return {
            "username": account["username"],
            "disabled": True,
            "sessions_revoked": revoked,
        }

    @app.post("/admin/users/{account_id}/enable")
    async def admin_enable_user(
        account_id: int, caller: Caller = Depends(require_admin)
    ):
        """Re-enable a disabled Account — audited."""
        account = db.get_account(config.db_path, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="No such Account.")
        if not account["disabled"]:
            raise HTTPException(
                status_code=400, detail="Account is not disabled."
            )
        db.set_disabled(config.db_path, account_id, False)
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="account_enabled",
            details={
                "account_id": account_id,
                "username": account["username"],
            },
        )
        return {"username": account["username"], "disabled": False}

    @app.post("/admin/users/{account_id}/revoke_sessions")
    async def admin_revoke_sessions(
        account_id: int, caller: Caller = Depends(require_admin)
    ):
        """Revoke every live Session of an Account, server-side — audited.

        The Account itself is untouched: it can simply log in again. This
        is the "kick everyone out now" lever for a compromised credential
        or a login shared too widely.
        """
        account = db.get_account(config.db_path, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="No such Account.")
        revoked = app.state.sessions.revoke_all_for_account(account_id)
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="sessions_revoked",
            details={
                "account_id": account_id,
                "username": account["username"],
                "revoked": revoked,
            },
        )
        return {
            "username": account["username"],
            "sessions_revoked": revoked,
        }

    @app.post("/admin/users/{account_id}/quota")
    async def admin_set_quota(
        account_id: int,
        request: QuotaAssignment,
        caller: Caller = Depends(require_admin),
    ):
        """Set an Account's Storage Quota override — an audited Admin action.

        ``quota_bytes`` is the cap in bytes, ``null`` to fall back to the
        global default, or ``-1`` (the ``QUOTA_UNLIMITED`` sentinel) to
        remove the Account's cap entirely. Admins are always exempt
        regardless of what is stored here.
        """
        account = db.get_account(config.db_path, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="No such Account.")
        db.set_quota_bytes(config.db_path, account_id, request.quota_bytes)
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="quota_assigned",
            details={
                "account_id": account_id,
                "username": account["username"],
                "quota_bytes": request.quota_bytes,
            },
        )
        updated = db.get_account(config.db_path, account_id)
        return {
            "username": account["username"],
            **quota_view(updated, config),
        }

    @app.get("/admin/audit")
    async def admin_audit(
        caller: Caller = Depends(require_admin),
        actor: str | None = Query(default=None),
        action: str | None = Query(default=None),
        start: str | None = Query(
            default=None,
            description="Inclusive lower bound (ISO date or datetime, UTC).",
        ),
        end: str | None = Query(
            default=None,
            description="Inclusive upper bound (ISO date or datetime, UTC).",
        ),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        """Read the Audit Ledger, filtered, newest first.

        The Admin Panel's ledger page filters by Actor, action and date;
        ``actor`` and ``action`` match exactly, the date bounds are
        inclusive. Pages carry the unpaginated total so the view can show
        "entries X–Y of Z".
        """
        return db.query_audit(
            config.db_path,
            actor=actor,
            action=action,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    @app.get("/admin/services")
    async def admin_services(caller: Caller = Depends(require_admin)):
        """Every Subsystem with container state, probe and disable flag.

        The listing is label-filtered to this compose project inside the
        docker control; an Optional Subsystem recorded as disabled stays
        flagged even when its container is stopped or gone — the flag is
        the Admin's decision, not a container state.
        """
        statuses = docker_or_502(control.statuses)
        ports = subsystem_ports()
        services = [service_view(name, statuses, ports) for name in ports]
        return {"services": services}

    @app.post("/admin/services/{name}/{action}")
    async def admin_service_action(
        name: str, action: str, caller: Caller = Depends(require_admin)
    ):
        """Control one Subsystem — audited, label-filtered, name-checked
        (ADR-0003).

        Only the Subsystem names from ``docker-compose.yml`` are known; an
        unknown name is refused before any docker call happens, so the
        socket's root-equivalent power stays scoped to this stack.
        Disable/enable exist for the Optional Subsystem only: disable
        stops the Bot and records the decision (surviving restarts of
        everything), enable clears it and starts the container again —
        both gracefully when the container is not there to act on.
        """
        if name not in subsystem_ports():
            raise HTTPException(status_code=404, detail="No such Subsystem.")

        if action in CONTAINER_ACTIONS:
            state = docker_or_502(lambda: control.act(name, action))
            audited = f"subsystem_{action}"
        elif action == "disable":
            if name not in OPTIONAL_SUBSYSTEMS:
                raise HTTPException(
                    status_code=400,
                    detail="Only the Optional Subsystem can be disabled.",
                )
            state = docker_or_502(lambda: control.act(name, "stop"))
            db.set_service_disabled(config.db_path, name, True)
            audited = "subsystem_disabled"
        elif action == "enable":
            if name not in OPTIONAL_SUBSYSTEMS:
                raise HTTPException(
                    status_code=400,
                    detail="Only the Optional Subsystem can be disabled.",
                )
            state = docker_or_502(lambda: control.act(name, "start"))
            db.set_service_disabled(config.db_path, name, False)
            audited = "subsystem_enabled"
        else:
            raise HTTPException(status_code=400, detail="Unknown action.")

        db.record_audit(
            config.db_path,
            actor=caller.username,
            action=audited,
            details={"subsystem": name, "state": state},
        )
        logger.info(
            "Subsystem '%s': %s by %s (state now %s).",
            name, action, caller.username, state,
        )
        return {
            "name": name,
            "action": action,
            "state": state,
            "disabled": db.service_disabled(config.db_path, name),
        }

    @app.get("/admin/files")
    async def admin_files(
        path: str = Query(default=""), caller: Caller = Depends(require_admin)
    ):
        """One directory of the Media Root tree, through the manager.

        The API never touches files (ADR-0001): it gates the Admin Role
        and forwards the path; the manager owns the sandbox and the
        Protected Path guards. A listing is a read, not a consequential
        action — only the operations land in the Audit Ledger.
        """
        return forward_media_or_502("GET", "/files", params={"path": path})

    def audit_file_op(caller: Caller, action: str, details: dict) -> None:
        """Write the file operation's ledger row *before* forwarding.

        The spec's decision of record: the API enforces the Admin role and
        writes ledger rows before forwarding, so an operation is audited
        even when the manager refuses it (Protected Path, sandbox) or is
        unreachable. The row carries the op type and the source/target
        paths — the manager's answer adds nothing the Admin needs.
        """
        db.record_audit(
            config.db_path, actor=caller.username, action=action, details=details
        )

    @app.post("/admin/files/delete")
    async def admin_files_delete(
        request: FileDeleteRequest, caller: Caller = Depends(require_admin)
    ):
        """Hard-delete under the Media Root, through the manager — audited."""
        audit_file_op(
            caller, "file_delete", {"op": "delete", "source": request.path}
        )
        return forward_media_or_502(
            "POST", "/files/delete", json={"path": request.path}
        )

    @app.post("/admin/files/rename")
    async def admin_files_rename(
        request: FileRenameRequest, caller: Caller = Depends(require_admin)
    ):
        """Rename a path's leaf, through the manager — audited."""
        target = posixpath.join(
            posixpath.dirname(request.path), request.new_name
        )
        audit_file_op(
            caller,
            "file_rename",
            {"op": "rename", "source": request.path, "target": target},
        )
        return forward_media_or_502(
            "POST",
            "/files/rename",
            json={"path": request.path, "new_name": request.new_name},
        )

    @app.post("/admin/files/move")
    async def admin_files_move(
        request: FileMoveRequest, caller: Caller = Depends(require_admin)
    ):
        """Move a path under another directory, through the manager — audited."""
        target = posixpath.join(
            request.target_dir, posixpath.basename(request.path)
        )
        audit_file_op(
            caller,
            "file_move",
            {"op": "move", "source": request.path, "target": target},
        )
        return forward_media_or_502(
            "POST",
            "/files/move",
            json={"path": request.path, "target_dir": request.target_dir},
        )

    # --- VPN (ticket #10) -----------------------------------------------------
    #
    # The settings live in the openvpn directory the gluetun Subsystem
    # mounts (api.vpn); the panel restarts gluetun through the existing
    # Subsystems endpoint to apply them.

    def vpn_view(settings: VpnSettings) -> dict:
        """The settings a Frontend may see — never the password."""
        return {
            "provider": settings.provider,
            "openvpn_user": settings.openvpn_user,
            "server": settings.server,
        }

    def valid_vpn_field(value: str | None, label: str) -> str | None:
        """A provided-but-blank field is a mistake, not a clear."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise HTTPException(
                status_code=400, detail=f"A {label} was required."
            )
        return cleaned

    @app.get("/admin/vpn/status")
    async def admin_vpn_status(caller: Caller = Depends(require_admin)):
        """The VPN settings plus the current egress IP, for the VPN page.

        The probe runs from the API's own network namespace — gluetun's,
        shared by every Subsystem — so the answer is the egress the whole
        stack exits through right now. A failed probe reads "unknown",
        not an error.
        """
        settings = vpn.read_settings()
        return {
            **vpn_view(settings),
            "egress_ip": vpn.egress_ip(),
        }

    @app.post("/admin/vpn/config")
    async def admin_vpn_config(
        request: VpnConfigRequest, caller: Caller = Depends(require_admin)
    ):
        """Edit the VPN provider, credentials and server — audited.

        Writes the panel's settings file and, when the server changes, the
        OpenVPN config's server lines. Applying the change needs a gluetun
        restart; the panel invokes the existing Subsystems endpoint right
        after this succeeds.
        """
        settings = vpn.update_settings(
            provider=valid_vpn_field(request.provider, "provider"),
            openvpn_user=valid_vpn_field(request.openvpn_user, "OpenVPN user"),
            openvpn_password=valid_vpn_field(
                request.openvpn_password, "OpenVPN password"
            ),
            server=valid_vpn_field(request.server, "server"),
        )
        # The password never reaches the ledger.
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="vpn_config_changed",
            details={
                "provider": settings.provider,
                "openvpn_user": settings.openvpn_user,
                "server": settings.server,
            },
        )
        logger.info("VPN settings changed by %s.", caller.username)
        return {
            "message": "VPN settings saved. Restart gluetun to apply them.",
            **vpn_view(settings),
        }

    @app.post("/admin/vpn/test")
    async def admin_vpn_test(caller: Caller = Depends(require_admin)):
        """Probe the public exit IP and report it — audited.

        The Admin compares the answer against what their VPN provider's
        exit should look like; the stack has no per-provider exit table to
        check against (noted on the ticket).
        """
        settings = vpn.read_settings()
        egress_ip = vpn.egress_ip()
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="vpn_test",
            details={
                "egress_ip": egress_ip,
                "provider": settings.provider,
                "server": settings.server,
            },
        )
        return {"egress_ip": egress_ip, **vpn_view(settings)}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=9870)
