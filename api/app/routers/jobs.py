import json
import uuid
from asyncio import get_event_loop
from datetime import datetime

import structlog
from api.app.core.security import _validate_no_ssrf
from fastapi import APIRouter, Depends, HTTPException, Query, status
from nats.js import JetStreamContext
from pydantic import AnyHttpUrl, BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.constants import NATS_JOBS_RUN_SUBJECT
from app.core.db import get_db
from app.core.nats import get_jetstream
from app.core.rate_limit import check_rate_limit
from app.models.job import Job, JobStatus, OutputFormat
from app.models.user import User

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = structlog.get_logger()


class JobCreate(BaseModel):
    url: AnyHttpUrl
    output_format: OutputFormat = OutputFormat.html

    @field_validator("url", mode="after")
    @classmethod
    def url_to_str(cls, v: AnyHttpUrl) -> str:
        return str(v)


class JobResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    url: str
    status: JobStatus
    output_format: OutputFormat
    result_path: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_rate_limit),
    js: JetStreamContext = Depends(get_jetstream),
) -> Job:
    await get_event_loop().run_in_executor(None, _validate_no_ssrf, str(body.url))

    job = Job(
        user_id=user.id,
        url=body.url,
        output_format=body.output_format,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Publish to NATS after successful DB insert (ADR-001)
    # If NATS is unavailable, job stays as `pending` and can be retried later
    payload = json.dumps(
        {
            "job_id": str(job.id),
            "url": job.url,
            "output_format": job.output_format.value,
        }
    ).encode()
    await js.publish(NATS_JOBS_RUN_SUBJECT, payload)
    logger.info("job_created", job_id=str(job.id), user_id=str(user.id), url=job.url)

    return job


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Job]:
    stmt = (
        select(Job)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.delete("/{job_id}", response_model=JobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # No-op if already in a terminal state — cancelling a completed/failed/cancelled job is harmless
    terminal_states = {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}
    if job.status not in terminal_states:
        job.status = JobStatus.cancelled
        await db.commit()
        await db.refresh(job)
        logger.info("job_cancelled", job_id=str(job_id))

    return job
