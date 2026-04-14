import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from miniopy_async.api import Minio
from sqlalchemy import case, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.dependencies import get_current_admin_user
from app.core.db import get_db
from app.core.minio import get_minio
from app.core.redis import get_redis
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery
from app.schemas.admin import (
    AdminStatsResponse,
    AdminUserDetailResponse,
    AdminUserResponse,
    AdminWebhookDeliveryResponse,
    HistoricalStats,
    OperationalStats,
    TopUserByJobs,
)
from app.schemas.jobs import CancelJobResponse, JobResponse
from app.settings import settings

MINIO_STORAGE_CACHE_KEY = "scrapeflow:cache:minio_storage"
MINIO_STORAGE_CACHE_TTL = 300  # seconds

router = APIRouter(prefix="/admin", tags=["admin"])
logger = structlog.get_logger()


def _admin_jobs_with_latest_run_stmt(
    user_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    status: str | None = None,
    engine: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    """Build the jobs+latest_run query for admin endpoints.

    Adapts _jobs_with_latest_run_stmt from jobs.py with user_id as an
    optional filter (admins see all jobs) plus status and engine filters.

    Note: uses LATERAL (inner) join — jobs with zero runs are excluded,
    consistent with the user-facing GET /jobs behaviour.
    """
    latest_run_sq = (
        select(JobRun)
        .where(JobRun.job_id == Job.id)
        .order_by(JobRun.created_at.desc())
        .limit(1)
        .correlate(Job)
        .lateral()
    )
    latest_run = aliased(JobRun, latest_run_sq)
    stmt = select(Job, latest_run).join(latest_run, true()).order_by(Job.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(Job.user_id == user_id)
    if job_id is not None:
        stmt = stmt.where(Job.id == job_id)
    if status is not None:
        stmt = stmt.where(latest_run.status == status)
    if engine is not None:
        stmt = stmt.where(Job.engine == engine)
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return stmt


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[AdminUserResponse])
async def admin_list_users(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    email: str | None = Query(default=None),
) -> list[AdminUserResponse]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    if email is not None:
        stmt = stmt.where(User.email.ilike(f"%{email}%"))
    result = await db.execute(stmt)
    return [AdminUserResponse.model_validate(u) for u in result.scalars()]


@router.get("/users/{user_id}", response_model=AdminUserDetailResponse)
async def admin_get_user(
    user_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> AdminUserDetailResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    counts_result = await db.execute(
        select(JobRun.status, func.count(JobRun.id).label("cnt"))
        .join(Job, JobRun.job_id == Job.id)
        .where(Job.user_id == user_id)
        .group_by(JobRun.status)
    )
    job_counts: dict[str, int] = {str(r[0]): int(r[1]) for r in counts_result.all()}

    return AdminUserDetailResponse(
        id=user.id,
        email=user.email,
        is_admin=user.is_admin,
        created_at=user.created_at,
        job_counts=job_counts,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_user(
    user_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await db.delete(user)
    await db.commit()
    logger.info("admin_user_deleted", user_id=str(user_id), admin_id=str(admin.id))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=list[JobResponse])
async def admin_list_jobs(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    engine: str | None = Query(default=None),
) -> list[JobResponse]:
    stmt = _admin_jobs_with_latest_run_stmt(
        user_id=user_id,
        status=status,
        engine=engine,
        limit=limit,
        offset=offset,
    )
    result = await db.execute(stmt)
    return [
        JobResponse(
            id=job.id,
            user_id=job.user_id,
            url=job.url,
            output_format=job.output_format,
            created_at=job.created_at,
            updated_at=job.updated_at,
            run_id=run.id,
            status=run.status,
            result_path=run.result_path,
            diff_detected=run.diff_detected,
            error=run.error,
            completed_at=run.completed_at,
        )
        for job, run in result.all()
    ]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def admin_get_job(
    job_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    stmt = _admin_jobs_with_latest_run_stmt(job_id=job_id)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job, run = row
    return JobResponse(
        id=job.id,
        user_id=job.user_id,
        url=job.url,
        output_format=job.output_format,
        created_at=job.created_at,
        updated_at=job.updated_at,
        run_id=run.id,
        status=run.status,
        result_path=run.result_path,
        diff_detected=run.diff_detected,
        error=run.error,
        completed_at=run.completed_at,
    )


@router.delete("/jobs/{job_id}", response_model=CancelJobResponse)
async def admin_delete_or_cancel_job(
    job_id: uuid.UUID,
    hard_delete: bool = Query(default=False),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> CancelJobResponse:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if hard_delete:
        await db.delete(job)
        await db.commit()
        logger.info("admin_job_hard_deleted", job_id=str(job_id), admin_id=str(admin.id))
        return CancelJobResponse(message="Job deleted")

    active_statuses = ("pending", "running", "processing")
    result = await db.execute(
        select(JobRun).where(JobRun.job_id == job_id).where(JobRun.status.in_(active_statuses))
    )
    active_runs = result.scalars().all()

    if not active_runs:
        return CancelJobResponse(message="Job has no active run to cancel")

    for run in active_runs:
        run.status = "cancelled"
    await db.commit()
    logger.info("admin_job_cancelled", job_id=str(job_id), admin_id=str(admin.id))
    return CancelJobResponse(message="Job run cancelled")


# ---------------------------------------------------------------------------
# Webhook deliveries
# ---------------------------------------------------------------------------


@router.get("/webhooks/deliveries", response_model=list[AdminWebhookDeliveryResponse])
async def admin_list_webhook_deliveries(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
) -> list[AdminWebhookDeliveryResponse]:
    stmt = (
        select(WebhookDelivery)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(WebhookDelivery.status == status)
    result = await db.execute(stmt)
    return [AdminWebhookDeliveryResponse.model_validate(d) for d in result.scalars()]


@router.post(
    "/webhooks/deliveries/{delivery_id}/retry",
    response_model=AdminWebhookDeliveryResponse,
)
async def admin_retry_webhook_delivery(
    delivery_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> AdminWebhookDeliveryResponse:
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook delivery not found"
        )
    delivery.attempts = 0
    delivery.status = "pending"
    delivery.next_attempt_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(delivery)
    logger.info(
        "admin_webhook_delivery_retried",
        delivery_id=str(delivery_id),
        admin_id=str(admin.id),
    )
    return AdminWebhookDeliveryResponse.model_validate(delivery)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


async def _build_operational_stats(
    db: AsyncSession,
    user_id: uuid.UUID | None = None,
) -> OperationalStats:
    """Compute live operational counts, optionally scoped to one user."""

    def _jr_user_scope(stmt):
        if user_id is not None:
            return stmt.join(Job, JobRun.job_id == Job.id).where(Job.user_id == user_id)
        return stmt

    def _wd_user_scope(stmt):
        if user_id is not None:
            return stmt.join(Job, WebhookDelivery.job_id == Job.id).where(Job.user_id == user_id)
        return stmt

    def _job_user_scope(stmt):
        if user_id is not None:
            return stmt.where(Job.user_id == user_id)
        return stmt

    jobs_running = (
        await db.execute(
            _jr_user_scope(
                select(func.count()).select_from(JobRun).where(JobRun.status == "running")
            )
        )
    ).scalar_one()

    jobs_pending = (
        await db.execute(
            _jr_user_scope(
                select(func.count()).select_from(JobRun).where(JobRun.status == "pending")
            )
        )
    ).scalar_one()

    engine_stmt = (
        select(Job.engine, func.count(JobRun.id).label("cnt"))
        .select_from(JobRun)
        .join(Job, JobRun.job_id == Job.id)
        .where(JobRun.status.in_(["running", "pending"]))
        .group_by(Job.engine)
    )
    if user_id is not None:
        engine_stmt = engine_stmt.where(Job.user_id == user_id)
    jobs_by_engine: dict[str, int] = {
        str(r[0]): int(r[1]) for r in (await db.execute(engine_stmt)).all()
    }

    wh_pending = (
        await db.execute(
            _wd_user_scope(
                select(func.count())
                .select_from(WebhookDelivery)
                .where(WebhookDelivery.status == "pending")
            )
        )
    ).scalar_one()

    wh_exhausted = (
        await db.execute(
            _wd_user_scope(
                select(func.count())
                .select_from(WebhookDelivery)
                .where(WebhookDelivery.status == "exhausted")
            )
        )
    ).scalar_one()

    active_recurring = (
        await db.execute(
            _job_user_scope(
                select(func.count()).select_from(Job).where(Job.schedule_status == "active")
            )
        )
    ).scalar_one()

    next_run = (
        await db.execute(
            _job_user_scope(
                select(func.min(Job.next_run_at)).where(
                    Job.schedule_status == "active",
                    Job.next_run_at.is_not(None),
                )
            )
        )
    ).scalar_one()

    return OperationalStats(
        jobs_running=jobs_running,
        jobs_pending=jobs_pending,
        jobs_by_engine=jobs_by_engine,
        webhook_deliveries_pending=wh_pending,
        webhook_deliveries_exhausted=wh_exhausted,
        active_recurring_jobs=active_recurring,
        next_scheduled_run_at=next_run,
    )


async def _build_historical_stats(
    db: AsyncSession,
    redis_client: aioredis.Redis,
    minio_client: Minio,
    user_id: uuid.UUID | None = None,
) -> HistoricalStats:
    """Compute time-windowed historical stats, optionally scoped to one user."""
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    def _jr_user_scope(stmt):
        if user_id is not None:
            return stmt.join(Job, JobRun.job_id == Job.id).where(Job.user_id == user_id)
        return stmt

    jobs_today = (
        await db.execute(
            _jr_user_scope(
                select(func.count()).select_from(JobRun).where(JobRun.created_at >= day_ago)
            )
        )
    ).scalar_one()

    jobs_this_week = (
        await db.execute(
            _jr_user_scope(
                select(func.count()).select_from(JobRun).where(JobRun.created_at >= week_ago)
            )
        )
    ).scalar_one()

    jobs_this_month = (
        await db.execute(
            _jr_user_scope(
                select(func.count()).select_from(JobRun).where(JobRun.created_at >= month_ago)
            )
        )
    ).scalar_one()

    status_stmt = (
        select(JobRun.status, func.count(JobRun.id).label("cnt"))
        .select_from(JobRun)
        .where(JobRun.created_at >= week_ago)
        .group_by(JobRun.status)
    )
    if user_id is not None:
        status_stmt = status_stmt.join(Job, JobRun.job_id == Job.id).where(Job.user_id == user_id)
    jobs_by_status_7d: dict[str, int] = {
        str(r[0]): int(r[1]) for r in (await db.execute(status_stmt)).all()
    }

    engine_7d_stmt = (
        select(Job.engine, func.count(JobRun.id).label("cnt"))
        .select_from(JobRun)
        .join(Job, JobRun.job_id == Job.id)
        .where(JobRun.created_at >= week_ago)
        .group_by(Job.engine)
    )
    if user_id is not None:
        engine_7d_stmt = engine_7d_stmt.where(Job.user_id == user_id)
    jobs_by_engine_7d: dict[str, int] = {
        str(r[0]): int(r[1]) for r in (await db.execute(engine_7d_stmt)).all()
    }

    # top_users_by_jobs is a leaderboard across all users — skip for per-user scope
    if user_id is None:
        top_stmt = (
            select(
                User.id.label("user_id"),
                User.email,
                func.count(JobRun.id).label("job_count"),
            )
            .select_from(JobRun)
            .join(Job, JobRun.job_id == Job.id)
            .join(User, Job.user_id == User.id)
            .where(JobRun.created_at >= week_ago)
            .group_by(User.id, User.email)
            .order_by(func.count(JobRun.id).desc())
            .limit(10)
        )
        top_rows = (await db.execute(top_stmt)).all()
        top_users_by_jobs = [
            TopUserByJobs(user_id=r.user_id, email=r.email, job_count=r.job_count) for r in top_rows
        ]
    else:
        top_users_by_jobs = []

    wh_stmt = (
        select(
            func.count(WebhookDelivery.id).label("total"),
            func.sum(case((WebhookDelivery.status == "delivered", 1), else_=0)).label("delivered"),
        )
        .select_from(WebhookDelivery)
        .where(WebhookDelivery.created_at >= week_ago)
    )
    if user_id is not None:
        wh_stmt = wh_stmt.join(Job, WebhookDelivery.job_id == Job.id).where(Job.user_id == user_id)
    wh_row = (await db.execute(wh_stmt)).one()
    wh_rate = wh_row.delivered / wh_row.total if wh_row.total > 0 else 0.0

    # MinIO storage bytes — Redis-cached for MINIO_STORAGE_CACHE_TTL seconds.
    # Per-user storage enumeration is impractical (one list_objects call per job),
    # so return 0 when scoped to a user.
    if user_id is not None:
        minio_bytes = 0
    else:
        cached = await redis_client.get(MINIO_STORAGE_CACHE_KEY)
        if cached is not None:
            minio_bytes = int(cached)
        else:
            minio_bytes = 0
            async for obj in minio_client.list_objects(settings.minio_bucket, recursive=True):
                minio_bytes += obj.size or 0
            await redis_client.setex(
                MINIO_STORAGE_CACHE_KEY, MINIO_STORAGE_CACHE_TTL, str(minio_bytes)
            )

    return HistoricalStats(
        jobs_today=jobs_today,
        jobs_this_week=jobs_this_week,
        jobs_this_month=jobs_this_month,
        jobs_by_status_7d=jobs_by_status_7d,
        jobs_by_engine_7d=jobs_by_engine_7d,
        top_users_by_jobs=top_users_by_jobs,
        minio_storage_bytes=minio_bytes,
        webhook_delivery_success_rate_7d=wh_rate,
    )


# ---------------------------------------------------------------------------
# Stats routes
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=AdminStatsResponse)
async def admin_get_stats(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
    minio_client: Minio = Depends(get_minio),
) -> AdminStatsResponse:
    operational = await _build_operational_stats(db)
    historical = await _build_historical_stats(db, redis_client, minio_client)
    return AdminStatsResponse(operational=operational, historical=historical)


@router.get("/stats/users/{user_id}", response_model=AdminStatsResponse)
async def admin_get_stats_user(
    user_id: uuid.UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
    minio_client: Minio = Depends(get_minio),
) -> AdminStatsResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    operational = await _build_operational_stats(db, user_id=user_id)
    historical = await _build_historical_stats(db, redis_client, minio_client, user_id=user_id)
    return AdminStatsResponse(operational=operational, historical=historical)
