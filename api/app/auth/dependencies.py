import httpx
import structlog
from clerk_backend_api.security.types import AuthenticateRequestOptions
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import API_KEY_PREFIX, verify_api_key
from app.auth.jwt import get_clerk, verify_request
from app.auth.user_sync import get_or_create_user
from app.core.db import get_db
from app.models.user import User
from app.settings import settings

# Extracts the X-API-Key header value (optional — returns None if missing)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
logger = structlog.get_logger()


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str | None = Depends(_api_key_header),
) -> User:
    """FastAPI dependency that authenticates via JWT or API key.

    Checks X-API-Key header first, falls back to Authorization: Bearer JWT.
    Raises HTTP 401 if neither is valid.

    Usage:
        @router.get("/jobs")
        async def list_jobs(user: User = Depends(get_current_user)):
            ...
    """
    # API key auth
    if api_key:
        user = await verify_api_key(db, api_key)
        if user:
            return user
        logger.warning("auth_failed", reason="invalid_api_key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    # JWT auth
    payload = await verify_request(request)
    return await get_or_create_user(db, payload)


def get_current_admin_user(user: User = Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Admin privileges required.",
        )
    return user


async def auth_from_token(token: str, db: AsyncSession) -> User | None:
    """Authenticate from a raw token string — used by WebSocket endpoints.

    REST endpoints receive tokens via the Authorization/X-API-Key header (handled by
    get_current_user). WebSocket clients cannot set arbitrary headers in the browser
    upgrade handshake, so the token arrives as a query parameter instead.
    """
    if token.startswith(API_KEY_PREFIX):
        return await verify_api_key(db, token)

    # Clerk JWT path — build a synthetic httpx.Request with an Authorization header
    # so the Clerk SDK's authenticate_request() can parse and verify the JWT.
    req = httpx.Request(
        method="GET",
        url="http://localhost/ws",
        headers={"Authorization": f"Bearer {token}"},
    )
    clerk = get_clerk()
    state = clerk.authenticate_request(
        req, AuthenticateRequestOptions(authorized_parties=settings.clerk_authorized_parties)
    )
    if not state.is_signed_in or not state.payload:
        return None
    return await get_or_create_user(db, state.payload)
