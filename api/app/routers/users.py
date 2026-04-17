import uuid
from asyncio import get_running_loop

import structlog
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import generate_api_key, hash_api_key
from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.core.security import validate_no_ssrf
from app.models.api_key import ApiKey
from app.models.llm_keys import UserLLMKey
from app.models.user import User
from app.schemas.users import (
    ApiKeyCreate,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    LLMKeyCreate,
    LLMKeyCreatedResponse,
    LLMKeyResponse,
    Providers,
    UserResponse,
)
from app.settings import settings

router = APIRouter(prefix="/users", tags=["users"])
logger = structlog.get_logger()


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
    try:
        await db.commit()
    except IntegrityError as err:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="API key name already exists"
        ) from err

    logger.info("api_key_created", key_id=str(api_key.id), user_id=str(user.id))
    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        created_at=api_key.created_at,
        revoked=api_key.revoked,
        key=raw_key,
    )


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


@router.post("/llm-keys", response_model=LLMKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_llm_keys(
    body: LLMKeyCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if body.base_url:
        await get_running_loop().run_in_executor(None, validate_no_ssrf, str(body.base_url))

    f = Fernet(settings.llm_key_encryption_key)
    encrypted_llm_key = f.encrypt(body.api_key.encode()).decode()

    new_llm_key = UserLLMKey(
        user_id=user.id,
        name=body.name,
        provider=body.provider,
        encrypted_api_key=encrypted_llm_key,
        base_url=body.base_url,
    )
    db.add(new_llm_key)
    await db.commit()
    await db.refresh(new_llm_key)

    return LLMKeyCreatedResponse(
        id=new_llm_key.id,
        name=body.name,
        provider=body.provider,
        base_url=new_llm_key.base_url,
        api_key=body.api_key[:4] + "*****",
    )


@router.get("/llm-keys", response_model=list[LLMKeyResponse])
async def get_llm_keys(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserLLMKey)
        .where(UserLLMKey.user_id == user.id)
        .order_by(UserLLMKey.created_at.desc())
    )
    llm_keys = result.scalars().all()

    return [
        LLMKeyResponse(
            id=llm_key.id,
            name=llm_key.name,
            provider=Providers(llm_key.provider),
            base_url=llm_key.base_url,
            created_at=llm_key.created_at,
        )
        for llm_key in llm_keys
    ]


@router.delete("/llm-keys/{llm_key_id}", response_model=LLMKeyResponse)
async def delete_llm_keys(
    llm_key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    llm_key = await db.get(UserLLMKey, llm_key_id)
    if llm_key is None or llm_key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LLM Key not found")

    await db.execute(delete(UserLLMKey).where(UserLLMKey.id == llm_key_id))
    await db.commit()

    return LLMKeyResponse(
        id=llm_key.id,
        name=llm_key.name,
        provider=Providers(llm_key.provider),
        base_url=llm_key.base_url,
        created_at=llm_key.created_at,
    )
