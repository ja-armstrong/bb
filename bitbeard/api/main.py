"""The Bitbeard API application.

The API is the only service a Frontend talks to. It owns identity, Sessions and
the Audit Ledger, and holds the docker socket for Admin Panel subsystem
control. It does not talk to qBittorrent or Jackett; media work belongs to the
Download Manager.

Authentication lives here at the boundary (ADR-0002): everything except
``/health``, the ``/auth`` endpoints, the public ``/register`` submission
endpoint and the token-authed ``/internal/events`` requires a valid Session
cookie, and admin endpoints additionally require the Admin Role. Frontend-service callers (the Bot) take the same user endpoints
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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Literal

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
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
    media_server,
    subsystem_ports,
)
from .jellyfin import (
    JellyfinClient,
    JellyfinProvisioningError,
    JellyfinUserMissingError,
    generate_password,
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

#: Actor recorded for public Registration Requests: the submitter has no
#: Account yet (and no Discord identity in this flow), so the ledger's
#: Actor field says "anonymous" and the details carry the source IP and
#: the requested username.
ANONYMOUS_ACTOR = "anonymous"

#: Per-IP throttle on the public Registration endpoint (ticket #29): one
#: address may submit at most five requests per hour, sliding window,
#: in-process. Simple and honest — a restart empties the buckets, and
#: the numbers live right here next to the enforcement.
REGISTRATION_LIMIT_PER_HOUR = 5
REGISTRATION_WINDOW_SECONDS = 3600.0


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
    #: Whether this Account has dismissed the Webui's welcome modal;
    #: None for synthetic Actors (no Account row behind them).
    tutorial_seen: bool | None = None
    #: How the Caller arrived: "session" (cookie) or "discord" (the Bot).
    via: str = "session"


class LoginRequest(BaseModel):
    username: str
    password: str


class RegistrationRequest(BaseModel):
    """An anonymous visitor's ask for an Account (ticket #29).

    The password is hashed immediately with the same mechanism Accounts
    use; the plaintext is never stored, logged or ledgared. The note is
    optional prose for the Admin who will review the request.
    """

    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    note: str | None = Field(default=None, max_length=2000)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class JellyfinPasswordSet(BaseModel):
    """The password a logged-in user sets on their own Jellyfin user
    (ticket #32).

    There is no current-password field: the call goes through the
    Jellyfin admin API key, which may set any user's password outright —
    so the random password generated at provisioning is overwritten
    without ever being involved. The Webui's confirm field is matched
    client-side, like the registration form's.
    """

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


def client_ip(request: Request) -> str:
    """The submitting visitor's address, for the per-IP throttle and ledger.

    The XFF contract (docs/proxy.md): the front proxy overwrites
    ``X-Forwarded-For`` with the client it saw, everything behind it
    passes the header through verbatim, so its first entry is the real
    client — which is what "per IP" means here (the API only ever sees
    direct sockets from inside the compose network).
    Direct callers fall back to the socket address.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def cookie_is_secure(config: Config, request: Request) -> bool:
    """Whether the Session cookie should carry the Secure flag.

    ``PUBLIC_HTTPS`` (default 1) says the stack is served behind the
    HTTPS reverse proxy, so the cookie is always Secure. The
    ``X-Forwarded-Proto: https`` header is the observed evidence and
    enables Secure too, whatever ``PUBLIC_HTTPS`` says — a request that
    really arrived over TLS is exactly where a Secure cookie belongs.
    ``PUBLIC_HTTPS=0`` without that evidence is the plain-HTTP escape
    hatch: no Secure flag, because browsers refuse to send Secure
    cookies over plain HTTP and logins would break silently.
    """
    if not config.public_https:
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        return forwarded_proto.split(",")[0].strip().lower() == "https"
    return True


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
    jellyfin_client: JellyfinClient | None = None,
    jellyfin_password: Callable[[], str] | None = None,
) -> FastAPI:
    """Build the API app. ``config``, the manager transport, the docker
    control, the VPN control, the Jellyfin client and the Jellyfin
    password generator are injectable for tests."""
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
    #: Per-IP submission buckets for the public ``/register`` endpoint:
    #: ``{ip: [monotonic timestamps]}``, pruned on access. In-process on
    #: purpose (see the constants above).
    app.state.registration_buckets = {}

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
            # USER-FACING
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
            tutorial_seen=bool(account["tutorial_seen"]),
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
                    # USER-FACING
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
                    tutorial_seen=bool(account["tutorial_seen"]),
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
            # USER-FACING
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

    # --- Jellyfin provisioning (ticket #31) ----------------------------------
    #
    # The optional half of the approve flow: when the integration is on
    # (JELLYFIN_URL + JELLYFIN_API_TOKEN set) and the Media Server is
    # Jellyfin, approving a Registration Request also creates the matching
    # Jellyfin user. The client and the password generator are injectable
    # for tests; the generated password never enters a log, a response or
    # the ledger.

    new_jellyfin_password = jellyfin_password or generate_password

    def jellyfin_client_for() -> JellyfinClient:
        """The client the provisioning calls go through: the injected
        one in tests, else the real one built from the config."""
        return jellyfin_client or JellyfinClient(
            config.jellyfin_url, config.jellyfin_api_token
        )

    def provision_jellyfin_user(username: str) -> dict:
        """Create the matching Jellyfin user for a newly approved Account.

        Never fails the approval: every outcome — succeeded, failed (with
        the reason) — comes back as the dict the Audit Ledger entry and
        the admin flash carry. The generated password is handed to the
        client and forgotten: it is not returned, logged or stored.
        """
        password = new_jellyfin_password()
        try:
            jellyfin_client_for().create_user(username, password)
        except JellyfinProvisioningError as exc:
            logger.warning(
                "Jellyfin provisioning for '%s' failed: %s", username, exc
            )
            return {"status": "failed", "reason": str(exc)}
        logger.info(
            "Jellyfin user '%s' created for the approved Account.", username
        )
        return {"status": "succeeded"}

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
                # USER-FACING
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
                # USER-FACING
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
            # USER-FACING
            raise HTTPException(status_code=404, detail="No such download.")
        if caller.account_id is not None:
            owner_mismatch = row["account"] != caller.account_id
        else:
            owner_mismatch = row["discord_name"] != caller.username
        if owner_mismatch:
            raise HTTPException(
                status_code=403,
                # USER-FACING
                detail="This download belongs to another Actor.",
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
                # USER-FACING
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
                    # USER-FACING
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
    async def login(request: LoginRequest, response: Response, http: Request):
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
                # USER-FACING
                status_code=401, detail="Invalid username or password."
            )
        if account["disabled"]:
            db.record_audit(
                config.db_path,
                actor=account["username"],
                action="login_failed",
                details={"reason": "account disabled"},
            )
            # USER-FACING
            raise HTTPException(status_code=403, detail="Account is disabled.")

        session = app.state.sessions.issue(account["id"])
        response.set_cookie(
            COOKIE_NAME,
            session.token,
            max_age=int(app.state.sessions.ttl.total_seconds()),
            httponly=True,
            samesite="lax",
            path="/",
            secure=cookie_is_secure(config, http),
        )
        db.record_audit(
            config.db_path, actor=account["username"], action="login", details={}
        )
        return {"role": account["role"]}

    @app.post("/register")
    async def register(request: RegistrationRequest, http: Request):
        """File a Registration Request from an anonymous visitor (ticket #29).

        The one unauthenticated write in the stack that is not the Bot's
        hop: the landing page's form lands here. The password is hashed
        through the same shared mechanism Accounts use the moment the
        request is accepted; the plaintext is never stored, logged or
        ledgared. The per-IP throttle runs before any store access so
        flooding stops at the boundary, and every accepted submission is
        audited with the anonymous Actor — the source IP rides in the
        ledger details.
        """
        ip = client_ip(http)

        # Throttle first, before the store: the attempt counts whether or
        # not the submission is accepted, which is the point — no cheap
        # probe of which usernames exist. The check-and-append never
        # awaits, so the event loop serialises it; no lock is needed.
        now = time.monotonic()
        buckets = app.state.registration_buckets
        window = [
            stamp for stamp in buckets.get(ip, [])
            if now - stamp < REGISTRATION_WINDOW_SECONDS
        ]
        if len(window) >= REGISTRATION_LIMIT_PER_HOUR:
            buckets[ip] = window
            raise HTTPException(
                status_code=429,
                # USER-FACING
                detail=(
                    "Too many registration requests have come from your "
                    "address. Please wait a while and try again."
                ),
            )
        buckets[ip] = [*window, now]

        username = request.username.strip()
        if not username:
            # USER-FACING
            raise HTTPException(status_code=400, detail="A username is required.")

        note = request.note
        if note is not None:
            note = note.strip() or None

        # Conflicts are named plainly at submission: an existing Account
        # and a pending request are different situations for the visitor.
        if db.find_account(config.db_path, username) is not None:
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail=(
                    "An Account with that username already exists. "
                    "Choose another username."
                ),
            )
        if db.pending_registration_request(config.db_path, username) is not None:
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail=(
                    "A request for that username is already pending. "
                    "Choose another username."
                ),
            )

        request_id = db.create_registration_request(
            config.db_path,
            username=username,
            password=request.password,
            note=note,
            source_ip=ip,
        )
        if request_id is None:
            # The store's UNIQUE constraint caught a race the pre-check
            # missed; the situation reads as a pending request now.
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail=(
                    "A request for that username is already pending. "
                    "Choose another username."
                ),
            )

        db.record_audit(
            config.db_path,
            actor=ANONYMOUS_ACTOR,
            action="registration_requested",
            details={"username": username, "source_ip": ip, "note": note},
        )
        logger.info(
            "Registration Request '%s' filed from %s.", username, ip
        )
        return {
            # USER-FACING
            "message": (
                "Request sent. An admin approves new accounts — there is "
                "no notification, so check back later and try logging in "
                "with the username and password you chose."
            ),
            "username": username,
        }

    @app.get("/auth/me")
    async def me(caller: Caller = Depends(require_session)):
        """Echo the calling Account, so Frontends can restore their view.

        ``jellyfin_password_available`` is the capability flag the Webui
        reads (ticket #32): the self-service password page shows only
        when the Jellyfin integration is on and the Media Server is
        Jellyfin. It is a single boolean — the page has nothing honest
        to show for the finer cases, and the endpoint itself names the
        reason on a real attempt.

        ``tutorial_seen`` drives the Webui's first-login welcome modal
        (shown until the Account dismisses it once) and
        ``jellyfin_url`` is the reachable link the modal's TO JELLYFIN
        button opens — only present when the capability flag is up,
        since a modal pointing at a dead URL is worse than no button.
        """
        jellyfin_available = (
            config.jellyfin_enabled and media_server() == "jellyfin"
        )
        return {
            "username": caller.username,
            "role": caller.role,
            "jellyfin_password_available": jellyfin_available,
            "jellyfin_url": (
                config.jellyfin_url if jellyfin_available else None
            ),
            "tutorial_seen": bool(caller.tutorial_seen),
        }

    @app.post("/me/tutorial-seen")
    async def me_tutorial_seen(caller: Caller = Depends(require_session)):
        """Mark the welcome modal as seen (first-login onboarding).

        The Webui calls this when the Account dismisses the modal; it is
        one-way and idempotent, so a replayed call changes nothing.
        Requires a Session (any Role): the flag is per-Account state,
        and an unauthenticated caller has no Account to mark. Audited
        like every Account-state change, so the Ledger shows who chose
        to skip the introductions.
        """
        db.set_tutorial_seen(config.db_path, caller.account_id)
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="tutorial_dismissed",
            details={},
        )
        return {"tutorial_seen": True}

    @app.post("/me/jellyfin-password")
    async def set_jellyfin_password(
        request: JellyfinPasswordSet, caller: Caller = Depends(require_session)
    ):
        """Set the calling Account's own Jellyfin password (ticket #32).

        The password lands on the Jellyfin user matching the caller's
        Bitbeard username, set through the admin API key — which needs
        no current password, so the random one generated at provisioning
        is overwritten without ever being asked for or shown. Requires a
        Session (any Role): an unauthenticated caller learns nothing
        here, and a logged-in one is told plainly why a request was
        refused — the integration being off, the Media Server not being
        Jellyfin, or no matching Jellyfin user existing. Audited without
        the password, like every password change.
        """
        if not config.jellyfin_enabled:
            raise HTTPException(
                status_code=403,
                # USER-FACING
                detail=(
                    "The Jellyfin integration is not set up on this "
                    "server, so there is no Jellyfin password to set."
                ),
            )
        if media_server() != "jellyfin":
            raise HTTPException(
                status_code=403,
                # USER-FACING
                detail=(
                    "This stack's Media Server is not Jellyfin, so there "
                    "is no Jellyfin password to set."
                ),
            )
        try:
            jellyfin_client_for().set_password(
                caller.username, request.new_password
            )
        except JellyfinUserMissingError:
            raise HTTPException(
                status_code=404,
                # USER-FACING
                detail=(
                    f"No Jellyfin user named '{caller.username}' exists "
                    "yet. Ask the Admin to create one."
                ),
            )
        except JellyfinProvisioningError as exc:
            logger.warning(
                "Jellyfin password set for '%s' failed: %s",
                caller.username, exc,
            )
            raise HTTPException(
                status_code=502,
                # USER-FACING
                detail="Jellyfin could not be reached. Try again in a bit.",
            )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="jellyfin_password_set",
            details={},
        )
        logger.info(
            "Jellyfin password set by '%s' from the Webui.", caller.username
        )
        return {
            # USER-FACING
            "message": "Password set — log into Jellyfin with it.",
        }

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
        # USER-FACING
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
                # USER-FACING
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
        # USER-FACING
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
                # USER-FACING
                status_code=400, detail="A Discord ID is required."
            )
        held = db.find_account_by_discord(config.db_path, discord_id)
        if held is not None and held["id"] != caller.account_id:
            raise HTTPException(
                status_code=400,
                # USER-FACING
                detail="That Discord ID is already linked to another account.",
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
        # USER-FACING
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
            # USER-FACING
            raise HTTPException(status_code=400, detail="A username is required.")
        if db.find_account(config.db_path, username) is not None:
            raise HTTPException(
                # USER-FACING
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
            # USER-FACING
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
                # USER-FACING
                status_code=400, detail="Account is already disabled."
            )
        if (
            account["role"] == "admin"
            and db.count_enabled_admins(config.db_path) <= 1
        ):
            raise HTTPException(
                status_code=400,
                # USER-FACING
                detail="dawg at least one admin. come on man.",
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
                # USER-FACING
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

    # --- Registration review (ticket #30) -----------------------------------
    #
    # The Admin's half of the flow the public /register endpoint started:
    # pending requests are listed, approved into working Accounts, or
    # denied. Both decisions are audited like every admin action.

    @app.get("/admin/registrations")
    async def admin_list_registrations(caller: Caller = Depends(require_admin)):
        """Every pending Registration Request, for the Admin Panel's
        Registrations page.

        A listing is a read, not a consequential action — the decisions
        (approve, deny) are what land in the Audit Ledger. The stored
        password hash never leaves the store: the panel has no use for
        it and the ledger must never carry it.
        """
        requests = [
            {
                "id": row["id"],
                "username": row["username"],
                "note": row["note"],
                "created_at": row["created_at"],
                "source_ip": row["source_ip"],
            }
            for row in db.list_registration_requests(config.db_path)
        ]
        return {"requests": requests}

    @app.post("/admin/registrations/{request_id}/approve")
    async def admin_approve_registration(
        request_id: int, caller: Caller = Depends(require_admin)
    ):
        """Approve a Registration Request: create the Account it asked
        for — audited.

        The stored username and password hash become a User Account
        directly, so the credentials the visitor chose at submission log
        in immediately — no reset ceremony. Because time may have passed,
        the username is re-checked at the moment of approval: an Account
        (or a newer pending request) may have taken it since. On conflict
        the approval is refused with the situation named plainly, so the
        Admin can deny the stale request instead; nothing is written.
        When the Jellyfin provisioning integration is on and the Media
        Server is Jellyfin, approving also creates the matching Jellyfin
        user — that outcome is recorded but can never fail the approval.
        """
        row = db.get_registration_request(config.db_path, request_id)
        if row is None:
            # USER-FACING
            raise HTTPException(
                status_code=404, detail="No such Registration Request."
            )
        username = row["username"]

        if db.find_account(config.db_path, username) is not None:
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail=(
                    f"An Account with the username '{username}' already "
                    "exists now. Deny this stale request."
                ),
            )
        clash = db.pending_registration_request(config.db_path, username)
        if clash is not None and clash["id"] != request_id:
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail=(
                    f"A newer request for the username '{username}' is "
                    "pending. Deny this stale request."
                ),
            )

        account_id = db.create_account_from_hash(
            config.db_path,
            username=username,
            password_hash=row["password_hash"],
            role="user",
        )
        if account_id is None:
            # The store's UNIQUE constraint caught a race the pre-check
            # missed; the situation reads as an existing Account now.
            raise HTTPException(
                status_code=409,
                # USER-FACING
                detail=(
                    f"An Account with the username '{username}' already "
                    "exists now. Deny this stale request."
                ),
            )
        db.delete_registration_request(config.db_path, request_id)

        # Optional Jellyfin provisioning (ticket #31): on only when both
        # JELLYFIN_URL and JELLYFIN_API_TOKEN are set and the Media Server
        # is Jellyfin. Never fails the approval — the outcome rides in the
        # ledger entry and, on failure, the admin flash.
        provisioning = {"status": "off"}
        if config.jellyfin_enabled and media_server() == "jellyfin":
            provisioning = provision_jellyfin_user(username)

        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="registration_approved",
            details={
                "registration_request_id": request_id,
                "account_id": account_id,
                "username": username,
                "source_ip": row["source_ip"],
                "jellyfin_provisioning": provisioning,
            },
        )
        logger.info(
            "Registration Request %d ('%s') approved by %s; Account %d "
            "created.",
            request_id, username, caller.username, account_id,
        )
        message = (
            # USER-FACING
            f"Approved. '{username}' can now log in with the "
            "credentials they chose."
        )
        if provisioning["status"] == "failed":
            message += (
                # USER-FACING
                f" Warning: the matching Jellyfin user was NOT created "
                f"({provisioning['reason']}). The Account works, but the "
                "user will need to be created on the Media Server by hand."
            )
        elif provisioning["status"] == "succeeded":
            message += (
                # USER-FACING
                " A matching Jellyfin user was created for them; they set "
                "their own Media Server password from the Webui later."
            )
        return {
            "message": message,
            "account_id": account_id,
            "username": username,
            "jellyfin_provisioning": provisioning,
        }

    @app.post("/admin/registrations/{request_id}/deny")
    async def admin_deny_registration(
        request_id: int, caller: Caller = Depends(require_admin)
    ):
        """Deny a Registration Request: remove it — audited.

        The pending row is the whole request, so denying is deleting it;
        the ledger keeps the decision (and the username and source IP
        the request carried) after the row is gone.
        """
        row = db.delete_registration_request(config.db_path, request_id)
        if row is None:
            # USER-FACING
            raise HTTPException(
                status_code=404, detail="No such Registration Request."
            )
        db.record_audit(
            config.db_path,
            actor=caller.username,
            action="registration_denied",
            details={
                "registration_request_id": request_id,
                "username": row["username"],
                "source_ip": row["source_ip"],
            },
        )
        logger.info(
            "Registration Request %d ('%s') denied by %s.",
            request_id, row["username"], caller.username,
        )
        return {
            # USER-FACING
            "message": f"Denied. The request from '{row['username']}' was removed.",
            "username": row["username"],
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
            # USER-FACING
            raise HTTPException(status_code=404, detail="No such subsystem.")

        if action in CONTAINER_ACTIONS:
            state = docker_or_502(lambda: control.act(name, action))
            audited = f"subsystem_{action}"
        elif action == "disable":
            if name not in OPTIONAL_SUBSYSTEMS:
                raise HTTPException(
                    status_code=400,
                    # USER-FACING
                    detail="Only optional subsystems can be disabled.",
                )
            state = docker_or_502(lambda: control.act(name, "stop"))
            db.set_service_disabled(config.db_path, name, True)
            audited = "subsystem_disabled"
        elif action == "enable":
            if name not in OPTIONAL_SUBSYSTEMS:
                raise HTTPException(
                    status_code=400,
                    detail="Only optional subsystems can be disabled.",
                )
            state = docker_or_502(lambda: control.act(name, "start"))
            db.set_service_disabled(config.db_path, name, False)
            audited = "subsystem_enabled"
        else:
            # USER-FACING
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

    # --- VPN (tickets #10 and #22) --------------------------------------------
    #
    # The settings live in the openvpn directory the gluetun Subsystem
    # mounts (api.vpn); the panel restarts gluetun through the existing
    # Subsystems endpoint to apply them. In the custom provider's mode the
    # server-lines rewrite reaches gluetun on that restart; in native mode
    # and for provider/credential changes in both modes, the operator
    # mirrors the settings into the compose .env and runs `docker compose
    # up` (a container's env is fixed at creation).

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
                # USER-FACING
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

        Writes the panel's settings file and, when the provider is the
        custom one, the OpenVPN config's server lines (a gluetun restart
        re-reads that file). In native mode there is no config file in
        play: the server is recorded in the settings file for the operator
        to carry into the compose .env, like provider and credential
        changes in both modes. The panel invokes the existing Subsystems
        endpoint right after this succeeds.
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
            # USER-FACING
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
