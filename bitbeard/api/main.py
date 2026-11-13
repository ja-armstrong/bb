"""The Bitbeard API application.

The API is the only service a Frontend talks to. It owns identity, Sessions and
the Audit Ledger, and holds the docker socket for Admin Panel subsystem
control. It does not talk to qBittorrent or Jackett; media work belongs to the
Download Manager.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared import db

from .config import Config, load_config

logger = logging.getLogger(__name__)


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

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "api"}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=9870)
