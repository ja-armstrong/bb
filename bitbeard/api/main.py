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
(Search, Download submission, progress, cancel) is proxied to the Download
Manager with the shared SERVICE_TOKEN and the acting Account as a trusted
header — the API never touches qBittorrent or Jackett itself.

The API also owns the Storage Quota decision (it owns identity and the cap
configuration): every Download submission is pre-checked against the
Actor's projected usage (on-disk plus In-flight) before it is proxied, and
Admins may set per-Account caps (override / unlimited / global default).
The manager maintains the usage counter those checks read.
"""

import logging
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
from .manager_proxy import ManagerProxy, ManagerUnavailable, Transport
from .sessions import SessionStore

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


def create_app(config: Config | None = None, proxy_transport: Transport | None = None) -> FastAPI:
    """Build the API app. ``config`` and the manager transport are
    injectable for tests."""
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

    @app.get("/admin/ping")
    async def admin_ping(caller: Caller = Depends(require_admin)):
        """Admin-role gate proof; real admin endpoints come with the panel."""
        return {"message": "pong", "admin": caller.username}

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

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=9870)
