"""The Bitbeard API application.

The API is the only service a Frontend talks to. It owns identity, Sessions and
the Audit Ledger, and holds the docker socket for Admin Panel subsystem
control. It does not talk to qBittorrent or Jackett; media work belongs to the
Download Manager.

Authentication lives here at the boundary (ADR-0002): everything except
``/health`` and the ``/auth`` endpoints requires a valid Session cookie, and
admin endpoints additionally require the Admin Role.
"""

import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from shared import db
from shared.security import verify_password

from .config import Config, load_config
from .sessions import SessionStore

logger = logging.getLogger(__name__)

#: The HTTP-only cookie that carries the Session token.
COOKIE_NAME = "bitbeard_session"


@dataclass(frozen=True)
class Caller:
    """The Account behind a request and the Session token it presented."""

    token: str
    account: sqlite3.Row


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


def create_app(config: Config | None = None) -> FastAPI:
    """Build the API app. ``config`` is injectable for tests."""
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
        return Caller(token=session.token, account=account)

    def require_admin(caller: Caller = Depends(require_session)) -> Caller:
        """Refuse non-Admin Accounts at the boundary."""
        if caller.account["role"] != "admin":
            raise HTTPException(status_code=403, detail="Admin Role required.")
        return caller

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
            config.db_path, actor=account["username"], action="login"
        )
        return {"role": account["role"]}

    @app.get("/auth/me")
    async def me(caller: Caller = Depends(require_session)):
        """Echo the calling Account, so Frontends can restore their view."""
        return {
            "username": caller.account["username"],
            "role": caller.account["role"],
        }

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
        if not verify_password(
            request.current_password, caller.account["password_hash"]
        ):
            raise HTTPException(
                status_code=400, detail="Current password is incorrect."
            )
        db.update_password(config.db_path, caller.account["id"], request.new_password)
        app.state.sessions.revoke_all_for_account(
            caller.account["id"], keep=caller.token
        )
        db.record_audit(
            config.db_path,
            actor=caller.account["username"],
            action="password_changed",
        )
        return {"message": "Password updated."}

    @app.get("/admin/ping")
    async def admin_ping(caller: Caller = Depends(require_admin)):
        """Admin-role gate proof; real admin endpoints come with the panel."""
        return {"message": "pong", "admin": caller.account["username"]}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=9870)
