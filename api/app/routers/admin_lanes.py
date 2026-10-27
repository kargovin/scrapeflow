"""Admin reads of every user's batches and crawls (E.10) — the owner-scoped routes' twins."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from miniopy_async import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.core.db import get_db
from app.core.minio import get_minio
from app.models.batch import Batch
from app.models.crawl import Crawl
from app.models.user import User
from app.routers.batch import item_result, list_items
from app.routers.crawls import list_pages, page_result
from app.schemas.batch import BatchItemResponse, BatchResponse
from app.schemas.crawls import CrawlPageResponse, CrawlResponse
from app.schemas.jobs import JobResultResponse

router = APIRouter(prefix="/admin", tags=["admin"])


def _with_email(response_type, row, email):
    return response_type.model_validate(row).model_copy(update={"user_email": email})


async def _one(db: AsyncSession, model, row_id: uuid.UUID, response_type, label: str):
    row = (
        await db.execute(
            select(model, User.email).join(User, User.id == model.user_id).where(model.id == row_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
    return _with_email(response_type, *row)


async def _list(db: AsyncSession, model, response_type, user_id, status_, limit, offset):
    stmt = (
        select(model, User.email)
        .join(User, User.id == model.user_id)
        .order_by(model.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if user_id is not None:
        stmt = stmt.where(model.user_id == user_id)
    if status_ is not None:
        stmt = stmt.where(model.status == status_)
    return [_with_email(response_type, row, email) for row, email in await db.execute(stmt)]


async def _exists(db: AsyncSession, model, row_id: uuid.UUID, label: str) -> None:
    if await db.get(model, row_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")


@router.get("/batches", response_model=list[BatchResponse])
async def admin_list_batches(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: uuid.UUID | None = Query(default=None),
    batch_status: str | None = Query(default=None, alias="status"),
) -> list[BatchResponse]:
    return await _list(db, Batch, BatchResponse, user_id, batch_status, limit, offset)


@router.get("/batches/{batch_id}", response_model=BatchResponse)
async def admin_get_batch(
    batch_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    return await _one(db, Batch, batch_id, BatchResponse, "Batch")


@router.get("/batches/{batch_id}/items", response_model=list[BatchItemResponse])
async def admin_list_batch_items(
    batch_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[BatchItemResponse]:
    await _exists(db, Batch, batch_id, "Batch")
    return await list_items(db, batch_id, limit, offset)


@router.get("/batches/{batch_id}/items/{item_id}/result", response_model=JobResultResponse)
async def admin_get_batch_item_result(
    batch_id: uuid.UUID,
    item_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> JobResultResponse:
    return await item_result(db, minio_client, batch_id, item_id)


@router.get("/crawls", response_model=list[CrawlResponse])
async def admin_list_crawls(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: uuid.UUID | None = Query(default=None),
    crawl_status: str | None = Query(default=None, alias="status"),
) -> list[CrawlResponse]:
    return await _list(db, Crawl, CrawlResponse, user_id, crawl_status, limit, offset)


@router.get("/crawls/{crawl_id}", response_model=CrawlResponse)
async def admin_get_crawl(
    crawl_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> CrawlResponse:
    return await _one(db, Crawl, crawl_id, CrawlResponse, "Crawl")


@router.get("/crawls/{crawl_id}/pages", response_model=list[CrawlPageResponse])
async def admin_list_crawl_pages(
    crawl_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    page_status: str | None = Query(default=None, alias="status"),
) -> list[CrawlPageResponse]:
    await _exists(db, Crawl, crawl_id, "Crawl")
    return await list_pages(db, crawl_id, limit, offset, page_status)


@router.get("/crawls/{crawl_id}/pages/{page_id}/result", response_model=JobResultResponse)
async def admin_get_crawl_page_result(
    crawl_id: uuid.UUID,
    page_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> JobResultResponse:
    return await page_result(db, minio_client, crawl_id, page_id)
