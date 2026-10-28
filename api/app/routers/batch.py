import asyncio
import secrets
import uuid
from asyncio import get_running_loop

import redis.asyncio as aioredis
import structlog
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from miniopy_async import Minio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import auth_from_token, get_current_user
from app.core.db import get_db
from app.core.dispatch import cancel_batch as cancel_batch_rows
from app.core.dispatch import (
    cancel_batch_workflow,
    fail_unstarted_batch,
    start_batch,
)
from app.core.encryption import get_fernet
from app.core.job_notifier import WebSocketConnectionLimitExceeded
from app.core.minio import get_minio
from app.core.quota import Admit, admission
from app.core.rate_limit import check_rate_limit_n
from app.core.redis import get_redis
from app.core.security import validate_no_ssrf
from app.models.batch import Batch, BatchItem
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.user import User
from app.routers.jobs import read_stored_result
from app.schemas.batch import BatchCreate, BatchItemResponse, BatchResponse
from app.schemas.jobs import JobResultResponse
from app.workflows.client import lazy_client

router = APIRouter(prefix="/batch", tags=["batch"])
logger = structlog.get_logger()


async def check_batch_quota(
    body: BatchCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Admit:
    """FastAPI dependency — the admission create_batch awaits right before its insert.

    Monthly check uses runs=len(urls) so the full batch is pre-approved atomically.
    """
    return admission(user.id, db, runs=len(body.urls), storage=False)


@router.post("", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    fernet: Fernet = Depends(get_fernet),
    admit: Admit = Depends(check_batch_quota),
) -> BatchResponse:
    # SSRF-check all URLs synchronously — sync rejection is better UX than
    # silent per-item failure after rows have been created.
    loop = get_running_loop()
    for url in body.urls:
        await loop.run_in_executor(None, validate_no_ssrf, str(url))

    if body.webhook_url:
        await loop.run_in_executor(None, validate_no_ssrf, str(body.webhook_url))

    if body.llm_config:
        key_id = body.llm_config.llm_key_id
        user_llm_key = await db.get(UserLLMKey, key_id)
        if user_llm_key is None or user_llm_key.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found")
        if user_llm_key.base_url:
            await loop.run_in_executor(None, validate_no_ssrf, str(user_llm_key.base_url))

    # Deduct len(urls) quota units atomically — prevents partial-window exploits.
    await check_rate_limit_n(len(body.urls), user.id, redis)

    webhook_secret_encrypted: str | None = None
    if body.webhook_url:
        webhook_secret_encrypted = fernet.encrypt(secrets.token_hex(32).encode()).decode()

    await admit()
    batch = Batch(
        user_id=user.id,
        status="queued",
        output_format=body.output_format.value,
        engine=body.engine.value,
        webhook_url=body.webhook_url,
        webhook_secret=webhook_secret_encrypted,
        respect_robots=body.respect_robots,
        llm_config=body.llm_config.model_dump(mode="json") if body.llm_config else None,
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
    runs = [JobRun(batch_item_id=item.id, status="pending", lane="v2") for item in items]
    db.add_all(runs)
    await db.commit()

    # Refresh to get DB-assigned IDs
    for item, run in zip(items, runs, strict=True):
        await db.refresh(item)
        await db.refresh(run)

    try:
        await start_batch(batch, items, runs, lambda: lazy_client(request.app.state))
    except Exception as exc:
        # There is no recovery: a batch whose workflow did not start is failed.
        logger.exception("batch_start_failed", batch_id=str(batch.id))
        await fail_unstarted_batch(db, batch, runs, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"batch_id": str(batch.id), "error": runs[0].error},
        ) from None

    # Transition batch to "running" after dispatch — unless a fast batch has already closed.
    await db.execute(
        update(Batch).where(Batch.id == batch.id, Batch.status == "queued").values(status="running")
    )
    await db.commit()
    await db.refresh(batch)

    logger.info("batch_created", batch_id=str(batch.id), user_id=str(user.id), total=batch.total)
    return BatchResponse.model_validate(batch)


async def owned_batch(db: AsyncSession, batch_id: uuid.UUID, user: User) -> Batch:
    batch = await db.get(Batch, batch_id)
    if batch is None or batch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    return batch


async def list_items(
    db: AsyncSession, batch_id: uuid.UUID, limit: int, offset: int
) -> list[BatchItemResponse]:
    result = await db.execute(
        select(BatchItem)
        .where(BatchItem.batch_id == batch_id)
        .order_by(BatchItem.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return [BatchItemResponse.model_validate(item) for item in result.scalars()]


async def item_result(
    db: AsyncSession, minio_client: Minio, batch_id: uuid.UUID, item_id: uuid.UUID
) -> JobResultResponse:
    item = await db.get(BatchItem, item_id)
    if item is None or item.batch_id != batch_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    if item.status != "completed" or item.result_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No completed result available"
        )
    return await read_stored_result(minio_client, item.result_path)


@router.get("", response_model=list[BatchResponse])
async def list_batches(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[BatchResponse]:
    result = await db.execute(
        select(Batch)
        .where(Batch.user_id == user.id)
        .order_by(Batch.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [BatchResponse.model_validate(b) for b in result.scalars()]


@router.get("/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    return BatchResponse.model_validate(await owned_batch(db, batch_id, user))


@router.get("/{batch_id}/items", response_model=list[BatchItemResponse])
async def list_batch_items(
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[BatchItemResponse]:
    await owned_batch(db, batch_id, user)
    return await list_items(db, batch_id, limit, offset)


@router.get("/{batch_id}/items/{item_id}/result", response_model=JobResultResponse)
async def get_batch_item_result(
    batch_id: uuid.UUID,
    item_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minio_client: Minio = Depends(get_minio),
) -> JobResultResponse:
    await owned_batch(db, batch_id, user)
    return await item_result(db, minio_client, batch_id, item_id)


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_batch(
    batch_id: uuid.UUID,
    request: Request,
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

    # Rows first: the v1 result consumer discards results for cancelled runs, and a v2 child's
    # next mirror refuses to move one. The engine is told after, best effort.
    runs = await cancel_batch_rows(db, batch_id)
    if runs is None:
        await db.refresh(batch)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch is already {batch.status}",
        )
    await cancel_batch_workflow(batch_id, runs, lambda: lazy_client(request.app.state))
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
    try:
        async with notifier.subscribe_batch(batch_id_str, str(user.id)) as queue:
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=300)
                except TimeoutError:
                    await websocket.send_json({"type": "timeout"})
                    break
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
    except WebSocketConnectionLimitExceeded:
        await websocket.close(code=4029, reason="connection limit exceeded")
        return

    await websocket.close()
