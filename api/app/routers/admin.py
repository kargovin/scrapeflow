import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.dependencies import get_current_admin_user
from app.core.db import get_db
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery
from app.schemas.admin import (
    AdminUserDetailResponse,
    AdminUserResponse,
    AdminWebhookDeliveryResponse,
)
from app.schemas.jobs import CancelJobResponse, JobResponse

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
    job_counts = dict(counts_result.all())  # type: ignore[arg-type]

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
