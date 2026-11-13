"""The SERVICE_TOKEN bearer gate shared by every service-to-service hop.

ADR-0002: the API<->manager hop and the manager->Bot webhook authenticate
with the same shared token from ``.env``. Every listener that accepts
internal traffic builds its dependency here, so the check exists exactly
once and cannot drift between services.
"""

from fastapi import Header, HTTPException


def require_service_token(service_token: str):
    """Build a FastAPI dependency that refuses requests without the token.

    ``service_token`` is bound at app construction, so a dependency instance
    always compares against the config its own app was built with.
    """
    def dependency(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {service_token}":
            raise HTTPException(status_code=401, detail="Invalid service token")

    return dependency
