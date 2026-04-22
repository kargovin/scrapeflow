import json
import uuid
from asyncio import get_running_loop
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from nats.js import JetStreamContext
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.core.db import get_db
from app.core.nats import get_jetstream
from app.core.rate_limit import check_rate_limit_n
from app.core.redis import get_redis
from app.core.security import validate_no_ssrf
from app.models.batch import Batch, BatchItem
from app.models.job_runs import JobRun
from app.models.user import User
from app.schemas.batch import BatchCreate, BatchItemResponse, BatchResponse
from app.schemas.jobs import Engine

router = APIRouter(prefix="/batch", tags=["batch"])
logger = structlog.get_logger()


@router.post("", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    js: JetStreamContext = Depends(get_jetstream),
    redis: aioredis.Redis = Depends(get_redis),
) -> BatchResponse:
    # SSRF-check all URLs synchronously — sync rejection is better UX than
    # silent per-item failure after rows have been created.
    loop = get_running_loop()
    for url in body.urls:
        await loop.run_in_executor(None, validate_no_ssrf, url)

    if body.webhook_url:
        await loop.run_in_executor(None, validate_no_ssrf, body.webhook_url)

    # Deduct len(urls) quota units atomically — prevents partial-window exploits.
    await check_rate_limit_n(len(body.urls), user.id, redis)

    batch = Batch(
        user_id=user.id,
        status="queued",
        output_format=body.output_format,
        engine=body.engine.value,
        webhook_url=body.webhook_url,
        respect_robots=body.respect_robots,
        total=len(body.urls),
        completed=0,
        failed=0,
    )
    db.add(batch)
    await db.flush()

    items = [BatchItem(batch_id=batch.id, url=url, status="pending") for url in body.urls]
    db.add_all(items)
    await db.flush()

    # One job_run per item — batch_item_id set, job_id null (ADR-006 §4).
    runs = [JobRun(batch_item_id=item.id, status="pending") for item in items]
    db.add_all(runs)
    await db.commit()

    # Refresh to get DB-assigned IDs
    for item, run in zip(items, runs, strict=False):
        await db.refresh(item)
        await db.refresh(run)

    # Dispatch one NATS message per item — identical fat-message schema, no batch_item_id.
    # Workers are unaware they are running a batch job (ADR-006 §4).
    subject = (
        NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
        if body.engine == Engine.playwright
        else NATS_JOBS_RUN_HTTP_SUBJECT
    )
    for item, run in zip(items, runs, strict=False):
        payload: dict[str, Any] = {
            "schema_version": 2,
            "job_id": None,
            "run_id": str(run.id),
            "url": item.url,
            "output_format": body.output_format.value,
            "engine": body.engine.value,
            "credentials": None,
            "options": {"respect_robots": body.respect_robots, "actions": None},
            "crawl_context": None,
        }
        await js.publish(subject, json.dumps(payload).encode())

    # Transition batch to "running" after first dispatch.
    await db.execute(update(Batch).where(Batch.id == batch.id).values(status="running"))
    await db.commit()
    await db.refresh(batch)

    logger.info("batch_created", batch_id=str(batch.id), user_id=str(user.id), total=batch.total)
    return BatchResponse.model_validate(batch)


@router.get("/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    batch = await db.get(Batch, batch_id)
    if batch is None or batch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    return BatchResponse.model_validate(batch)


@router.get("/{batch_id}/items", response_model=list[BatchItemResponse])
async def list_batch_items(
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[BatchItemResponse]:
    batch = await db.get(Batch, batch_id)
    if batch is None or batch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    result = await db.execute(
        select(BatchItem)
        .where(BatchItem.batch_id == batch_id)
        .order_by(BatchItem.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return [BatchItemResponse.model_validate(item) for item in result.scalars()]


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_batch(
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    batch = await db.get(Batch, batch_id)
    if batch is None or batch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    if batch.status in ("completed", "partial_failure", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch is already {batch.status}",
        )

    # Cancel all pending/running items — result consumer discards results for cancelled runs.
    pending_items_result = await db.execute(
        select(BatchItem).where(
            BatchItem.batch_id == batch_id,
            BatchItem.status.in_(("pending", "running")),
        )
    )
    pending_items = pending_items_result.scalars().all()
    for item in pending_items:
        item.status = "cancelled"

    # Cancel the corresponding job_runs so result consumer ignores their results.
    if pending_items:
        item_ids = [item.id for item in pending_items]
        pending_runs_result = await db.execute(
            select(JobRun).where(
                JobRun.batch_item_id.in_(item_ids),
                JobRun.status.in_(("pending", "running")),
            )
        )
        for run in pending_runs_result.scalars().all():
            run.status = "cancelled"

    batch.status = "cancelled"
    await db.commit()
    logger.info("batch_cancelled", batch_id=str(batch_id), user_id=str(user.id))
