import json
import uuid
from asyncio import get_running_loop
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from nats.js import JetStreamContext
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import auth_from_token, get_current_user
from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.core.db import get_db
from app.core.nats import get_jetstream
from app.core.quota import check_user_quota
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


async def check_batch_quota(
    body: BatchCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """FastAPI dependency — raises 429 if monthly_runs or concurrent_jobs quota is exceeded.

    Monthly check uses batch_count=len(urls) so the full batch is pre-approved atomically.
    """
    await check_user_quota(user.id, db, "monthly_runs", batch_count=len(body.urls))
    await check_user_quota(user.id, db, "concurrent_jobs")


@router.post("", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    js: JetStreamContext = Depends(get_jetstream),
    redis: aioredis.Redis = Depends(get_redis),
    _quota: None = Depends(check_batch_quota),
) -> BatchResponse:
    # SSRF-check all URLs synchronously — sync rejection is better UX than
    # silent per-item failure after rows have been created.
    loop = get_running_loop()
    for url in body.urls:
        await loop.run_in_executor(None, validate_no_ssrf, str(url))

    if body.webhook_url:
        await loop.run_in_executor(None, validate_no_ssrf, str(body.webhook_url))

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


_BATCH_TERMINAL_STATUSES = frozenset({"completed", "partial_failure", "cancelled"})


def _batch_progress_msg(batch: Batch) -> dict:
    return {
        "type": "batch_progress",
        "batch_id": str(batch.id),
        "total": batch.total,
        "completed": batch.completed,
        "failed": batch.failed,
        "status": batch.status,
    }


def _batch_terminal_msg(batch: Batch) -> dict:
    return {
        "type": batch.status,
        "batch_id": str(batch.id),
        "total": batch.total,
        "completed": batch.completed,
        "failed": batch.failed,
        "status": batch.status,
    }


@router.websocket("/{batch_id}/watch")
async def batch_status_stream(
    websocket: WebSocket,
    batch_id: uuid.UUID,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream live batch progress until the batch reaches a terminal state.

    Each message reports completed/failed/total counts and the most recently
    finished item. Auth and close-code semantics mirror /jobs/{id}/watch.
    """
    await websocket.accept()

    user = await auth_from_token(token, db)
    if user is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    batch = await db.get(Batch, batch_id)
    if batch is None or batch.user_id != user.id:
        await websocket.close(code=4004, reason="not found")
        return

    batch_id_str = str(batch_id)

    if batch.status in _BATCH_TERMINAL_STATUSES:
        await websocket.send_json(_batch_terminal_msg(batch))
        await websocket.close()
        return

    await websocket.send_json(_batch_progress_msg(batch))

    notifier = websocket.app.state.job_notifier
    async with notifier.subscribe_batch(batch_id_str) as queue:
        while True:
            update = await queue.get()
            new_status = update.get("status", "")
            msg: dict = {
                "type": "batch_progress"
                if new_status not in _BATCH_TERMINAL_STATUSES
                else new_status,
                "batch_id": batch_id_str,
                "total": update.get("total", batch.total),
                "completed": update.get("completed", batch.completed),
                "failed": update.get("failed", batch.failed),
                "status": new_status,
            }
            if update.get("item_url"):
                msg["latest_item"] = {
                    "url": update["item_url"],
                    "status": update.get("item_status", ""),
                }
            await websocket.send_json(msg)
            if new_status in _BATCH_TERMINAL_STATUSES:
                break

    await websocket.close()
