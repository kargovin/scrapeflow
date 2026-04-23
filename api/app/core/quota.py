"""Per-user quota enforcement (PRD-012).

Three dimensions:
  monthly_runs    — job_runs created since first-of-month
  concurrent_jobs — job_runs currently in pending/running/processing
  storage_bytes   — cumulative MinIO bytes tracked in user_quotas.storage_bytes_used

Limits come from the user_quotas row; NULL columns fall back to settings defaults.

check_user_quota()  — raises HTTP 429; use at API endpoints (POST /jobs, POST /batch)
is_quota_exceeded() — returns bool; use in scheduler (no HTTP context)
check_storage_quota() / increment_storage_bytes() / decrement_storage_bytes()
                    — used by result_consumer and cleanup_old_runs
"""

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.batch import Batch, BatchItem
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user_quota import UserQuota
from app.settings import settings

logger = structlog.get_logger()


def _first_of_next_month(now: datetime) -> datetime:
    if now.month == 12:
        return now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


async def _count_monthly_runs(user_id: uuid.UUID, db: AsyncSession) -> int:
    now = datetime.now(UTC)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    count = await db.scalar(
        select(func.count(JobRun.id))
        .outerjoin(Job, JobRun.job_id == Job.id)
        .outerjoin(BatchItem, JobRun.batch_item_id == BatchItem.id)
        .outerjoin(Batch, BatchItem.batch_id == Batch.id)
        .where(
            func.coalesce(Job.user_id, Batch.user_id) == user_id,
            JobRun.created_at >= first_of_month,
        )
    )
    return count or 0


async def _count_concurrent_jobs(user_id: uuid.UUID, db: AsyncSession) -> int:
    count = await db.scalar(
        select(func.count(JobRun.id))
        .outerjoin(Job, JobRun.job_id == Job.id)
        .outerjoin(BatchItem, JobRun.batch_item_id == BatchItem.id)
        .outerjoin(Batch, BatchItem.batch_id == Batch.id)
        .where(
            func.coalesce(Job.user_id, Batch.user_id) == user_id,
            JobRun.status.in_(["pending", "running", "processing"]),
        )
    )
    return count or 0


async def _quota_check(
    user_id: uuid.UUID,
    db: AsyncSession,
    quota_type: str,
    batch_count: int = 1,
) -> tuple[bool, dict]:
    """Returns (exceeded, detail_dict). detail_dict is empty when not exceeded."""
    quota_row = await db.get(UserQuota, user_id)
    now = datetime.now(UTC)

    if quota_type == "monthly_runs":
        used = await _count_monthly_runs(user_id, db)
        limit = (
            quota_row.monthly_runs_limit
            if quota_row and quota_row.monthly_runs_limit is not None
            else settings.default_quota_monthly_runs
        )
        if used + batch_count > limit:
            resets_at = _first_of_next_month(now).isoformat()
            return True, {
                "error": "quota_exceeded",
                "quota_type": "monthly_runs",
                "message": (
                    f"Monthly run limit reached ({used}/{limit}). "
                    f"Quota resets on {resets_at[:10]}."
                ),
                "resets_at": resets_at,
            }

    elif quota_type == "concurrent_jobs":
        used = await _count_concurrent_jobs(user_id, db)
        limit = (
            quota_row.concurrent_jobs_limit
            if quota_row and quota_row.concurrent_jobs_limit is not None
            else settings.default_quota_concurrent_jobs
        )
        if used + batch_count > limit:
            return True, {
                "error": "quota_exceeded",
                "quota_type": "concurrent_jobs",
                "message": (
                    f"Concurrent job limit reached ({used}/{limit}). " "Wait for a job to complete."
                ),
                "resets_at": None,
            }

    elif quota_type == "storage_bytes":
        used = quota_row.storage_bytes_used if quota_row else 0
        limit = (
            quota_row.storage_bytes_limit
            if quota_row and quota_row.storage_bytes_limit is not None
            else settings.default_quota_storage_bytes
        )
        if used >= limit:
            return True, {
                "error": "quota_exceeded",
                "quota_type": "storage_bytes",
                "message": (
                    f"Storage limit reached ({used}/{limit} bytes). "
                    "Delete old results to continue."
                ),
                "resets_at": None,
            }

    return False, {}


async def check_user_quota(
    user_id: uuid.UUID,
    db: AsyncSession,
    quota_type: str,
    batch_count: int = 1,
) -> None:
    """Raises HTTP 429 if the user has exceeded the given quota dimension.

    batch_count lets callers (e.g. POST /batch) check whether adding N items
    would push the user over their monthly_runs limit.
    """
    exceeded, detail = await _quota_check(user_id, db, quota_type, batch_count=batch_count)
    if exceeded:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


async def is_quota_exceeded(user_id: uuid.UUID, db: AsyncSession, quota_type: str) -> bool:
    """Returns True if the quota is exceeded. No HTTP exception raised.

    Used by the scheduler, which has no HTTP context and must not 429.
    """
    exceeded, _ = await _quota_check(user_id, db, quota_type)
    return exceeded


async def check_storage_quota(user_id: uuid.UUID, db: AsyncSession, additional_bytes: int) -> bool:
    """Returns True if the user can store additional_bytes more. False if over limit."""
    quota_row = await db.get(UserQuota, user_id)
    used = quota_row.storage_bytes_used if quota_row else 0
    limit = (
        quota_row.storage_bytes_limit
        if quota_row and quota_row.storage_bytes_limit is not None
        else settings.default_quota_storage_bytes
    )
    return (used + additional_bytes) <= limit


async def increment_storage_bytes(user_id: uuid.UUID, db: AsyncSession, size: int) -> None:
    """Upsert user_quotas and increment storage_bytes_used by size.

    Called within the result_consumer session — caller owns the commit.
    """
    await db.execute(
        text("""
            INSERT INTO user_quotas (user_id, storage_bytes_used, updated_at)
            VALUES (:user_id, :size, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET storage_bytes_used = user_quotas.storage_bytes_used + :size,
                updated_at = NOW()
        """),
        {"user_id": str(user_id), "size": size},
    )


async def decrement_storage_bytes(user_id: uuid.UUID, db: AsyncSession, size: int) -> None:
    """Decrement storage_bytes_used. Clamps to 0 to prevent negative values.

    Called by cleanup_old_runs after a successful MinIO delete.
    """
    await db.execute(
        text("""
            INSERT INTO user_quotas (user_id, storage_bytes_used, updated_at)
            VALUES (:user_id, 0, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET storage_bytes_used = GREATEST(0, user_quotas.storage_bytes_used - :size),
                updated_at = NOW()
        """),
        {"user_id": str(user_id), "size": size},
    )
