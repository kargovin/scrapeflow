import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import generate_api_key, hash_api_key
from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.models.api_key import ApiKey
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])
logger = structlog.get_logger()


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    revoked: bool

    model_config = {"from_attributes": True}


class ApiKeyCreatedResponse(ApiKeyResponse):
    # Raw key included only in the creation response — shown once, never stored.
    key: str


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    return user


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw_key = generate_api_key()
    api_key = ApiKey(
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        name=body.name,
    )
    db.add(api_key)
    await db.commit()

    # Attach the raw key to the ORM object so the response model can read it.
    # It is NOT persisted — only the hash is in the DB.
    api_key.key = raw_key
    logger.info("api_key_created", key_id=str(api_key.id), user_id=str(user.id))
    return api_key


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user.id)
        .where(ApiKey.revoked == False)  # noqa: E712
        .order_by(ApiKey.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/api-keys/{key_id}", response_model=ApiKeyResponse)
async def revoke_api_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    api_key = await db.get(ApiKey, key_id)

    # Return 404 for missing keys OR keys belonging to other users (same pattern as jobs).
    if api_key is None or api_key.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    api_key.revoked = True
    await db.commit()
    logger.info("api_key_revoked", key_id=str(key_id))
    return api_key
