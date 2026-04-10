import json
import secrets
import uuid
from asyncio import get_running_loop
from datetime import datetime, timedelta
from typing import Any

import structlog
from croniter import croniter
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from nats.js import JetStreamContext
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.dependencies import get_current_user
from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.core.db import get_db
from app.core.nats import get_jetstream
from app.core.rate_limit import check_rate_limit
from app.core.security import validate_no_ssrf
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.user import User
from app.schemas.jobs import CancelJobResponse, Engine, JobCreate, JobCreateResponse, JobResponse
from app.settings import settings

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = structlog.get_logger()


def validate_cron_min_interval(cron_expr: str, min_minutes: int) -> None:
    base = datetime.now()
    c = croniter(cron_expr, base)
    prev = c.get_next(datetime)
    cutoff = base + timedelta(minutes=min_minutes * 2)
    while prev < cutoff:
        curr = c.get_next(datetime)
        if (curr - prev).total_seconds() < min_minutes * 60:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Schedule interval must be at least {min_minutes} minutes",
            )
        prev = curr


def _jobs_with_latest_run_stmt(
    user_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    latest_run_sq = (
        select(JobRun)
        .where(JobRun.job_id == Job.id)
        .order_by(JobRun.created_at.desc())
        .limit(1)
        .correlate(Job)
        .lateral()
    )
    latest_run = aliased(JobRun, latest_run_sq)
    stmt = (
        select(Job, latest_run)
        .join(latest_run, true())
        .where(Job.user_id == user_id)
        .order_by(Job.created_at.desc())
    )
    if job_id is not None:
        stmt = stmt.where(Job.id == job_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return stmt


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    js: JetStreamContext = Depends(get_jetstream),
) -> JobCreateResponse:
    # Validate for SSRF for
    # 1) url to scrape
    # 2) Webhook Url
    # 3) LLM base url in case of Openai compatible url
    await get_running_loop().run_in_executor(None, validate_no_ssrf, str(body.url))

    if body.webhook_url:
        await get_running_loop().run_in_executor(None, validate_no_ssrf, str(body.webhook_url))

    if body.llm_config:
        key_id = body.llm_config.llm_key_id
        user_llm_key = await db.get(UserLLMKey, key_id)
        if user_llm_key is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LLM Key not found")
        if user_llm_key.base_url:
            await get_running_loop().run_in_executor(
                None, validate_no_ssrf, str(user_llm_key.base_url)
            )

    # validate cron exp and also validate min intervals constraint is not broken
    if body.schedule_cron:
        if not croniter.is_valid(body.schedule_cron):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid cron expression"
            )
        validate_cron_min_interval(body.schedule_cron, settings.schedule_min_interval_minutes)

    # Generate a token and encrypt it using Fernet
    webhook_secret_plain: str | None = None
    webhook_secret_encrypted: str | None = None
    if body.webhook_url:
        f = Fernet(settings.llm_key_encryption_key)
        webhook_secret_plain = secrets.token_hex(32)
        webhook_secret_encrypted = f.encrypt(webhook_secret_plain.encode()).decode()

    if body.engine == Engine.playwright and not body.playwight_options:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Playwright options missing"
        )

    job = Job(
        user_id=user.id,
        url=body.url,
        output_format=body.output_format,
        engine=body.engine,
        schedule_cron=body.schedule_cron,
        webhook_url=body.webhook_url,
        webhook_secret=webhook_secret_encrypted if body.webhook_url else None,
        llm_config=body.llm_config.model_dump(mode="json") if body.llm_config else None,
        playwright_options=body.playwight_options.model_dump() if body.playwight_options else None,
    )
    db.add(job)
    await db.flush()
    job_run = JobRun(job_id=job.id, status="pending")
    db.add(job_run)
    await db.commit()
    await db.refresh(job)
    await db.refresh(job_run)

    # Publish to NATS after successful DB insert (ADR-001)
    # If NATS is unavailable, job stays as `pending` and can be retried later
    payload: dict[str, Any] = {
        "job_id": str(job.id),
        "run_id": str(job_run.id),
        "url": job.url,
        "output_format": job.output_format.value,
    }

    if body.engine == Engine.http:
        await js.publish(NATS_JOBS_RUN_HTTP_SUBJECT, json.dumps(payload).encode())
    elif body.engine == Engine.playwright:
        payload["playwright_options"] = (
            body.playwight_options.model_dump() if body.playwight_options else None
        )
        await js.publish(NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT, json.dumps(payload).encode())
    logger.info("job_created", job_id=str(job.id), user_id=str(user.id), url=job.url)

    return JobCreateResponse(
        id=job.id,
        user_id=job.user_id,
        url=job.url,
        status=job_run.status,
        output_format=job.output_format,
        result_path=job_run.result_path,
        error=job_run.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        run_id=job_run.id,
        webhook_secret=webhook_secret_plain,
    )


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobResponse]:
    stmt = _jobs_with_latest_run_stmt(user.id, limit=limit, offset=offset)
    result = await db.execute(stmt)
    rows = result.all()
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
        for job, run in rows
    ]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    stmt = _jobs_with_latest_run_stmt(user.id, job_id=job_id)
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


@router.delete("/{job_id}", response_model=CancelJobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CancelJobResponse:
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

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
    logger.info("job_cancelled", job_id=str(job_id))
    return CancelJobResponse(message="Job run cancelled")
