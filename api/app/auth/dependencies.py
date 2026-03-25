from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import verify_api_key
from app.auth.jwt import verify_request
from app.auth.user_sync import get_or_create_user
from app.core.db import get_db
from app.models.user import User

# Extracts the X-API-Key header value (optional — returns None if missing)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    # JWT auth
    payload = await verify_request(request)
    return await get_or_create_user(db, payload)
