"""The scheduled-job admission activity — what ``_dispatch_due_jobs`` decides per job on v1, per tick.

Count and insert in one transaction under the user's lock (BUG-023): the ticks of one user's
schedules re-ask together when a slot frees, and only as many as fit may insert a run.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app.core.credentials import resolve_credentials
from app.core.db import AsyncSessionLocal
from app.core.dispatch import build_scrape_input
from app.core.quota import is_quota_exceeded, lock_user
from app.core.webhooks import create_webhook_delivery
from app.models.job import Job
from app.models.job_runs import JobRun
from app.workflows.job import JobRunInput
from app.workflows.scheduled_job import ADMIT_SCHEDULED_RUN_ACTIVITY, Admission, AdmitInput

logger = structlog.get_logger()

# ADR-010 §1: the error names the remedy — storage never frees itself.
STORAGE_FULL = (
    "storage_quota_exceeded: storage is full — delete old results to resume this schedule"
)


async def _admitted(db: AsyncSession, job: Job, run: JobRun) -> Admission:
    credentials = await resolve_credentials(job, db)
    return Admission(
        verdict="admitted",
        run=JobRunInput(
            run_id=str(run.id), engine=job.engine, scrape=build_scrape_input(job, run, credentials)
        ),
    )


async def _skip(db: AsyncSession, job: Job, reason: str, now: datetime) -> Admission:
    job.schedule_skipped_at = now
    job.schedule_skip_reason = reason
    job.schedule_waiting_since = None
    await db.commit()
    logger.warning("scheduled_tick_skipped", job_id=str(job.id), reason=reason)
    return Admission(verdict="skipped")


async def _fail(db: AsyncSession, job: Job, run_id: uuid.UUID, now: datetime) -> Admission:
    """A run that never fetched: a failed row, its webhook and notify, as a worker failure leaves."""
    run = JobRun(
        id=run_id,
        job_id=job.id,
        status="failed",
        error=STORAGE_FULL,
        completed_at=now,
        lane="v2",
    )
    db.add(run)
    await db.flush()
    job.last_run_at = now
    job.schedule_waiting_since = None
    if job.webhook_url and (not job.webhook_events or "job.failed" in job.webhook_events):
        await create_webhook_delivery(
            db, job, run.id, event="job.failed", minio_path=None, error=STORAGE_FULL
        )
    await db.execute(text("SELECT pg_notify('job_status', :p)"), {"p": f"{job.id}:{run.id}:failed"})
    await db.commit()
    logger.warning("scheduled_run_failed_storage_full", job_id=str(job.id), run_id=str(run.id))
    return Admission(verdict="failed")


@activity.defn(name=ADMIT_SCHEDULED_RUN_ACTIVITY)
async def admit_scheduled_run(input: AdmitInput) -> Admission:
    """Per meter (ADR-010 §1): monthly skips the tick, storage fails it, concurrency waits.

    A skip is recorded on the job, never as a ``job_runs`` row — the monthly meter counts every
    row. A storage failure is a run (v1's failure for the same breach, which a run also cost).
    """
    run_id = uuid.UUID(input.run_id)
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, uuid.UUID(input.job_id))
        if job is None:
            return Admission(verdict="stopped")
        await lock_user(job.user_id, db)

        # A retry of a call that committed: the run is this tick's whatever the job says now.
        run = await db.get(JobRun, run_id)
        if run is not None:
            return (
                Admission(verdict="failed")
                if run.status == "failed"
                else await _admitted(db, job, run)
            )

        # Paused, removed or handed back to v1 since the tick fired: the row is the truth.
        if (
            job.schedule_lane != "v2"
            or job.schedule_status != "active"
            or job.schedule_cron is None
        ):
            return Admission(verdict="stopped")

        now = datetime.now(UTC)
        if await is_quota_exceeded(job.user_id, db, "monthly_runs"):
            return await _skip(db, job, "monthly_runs", now)
        if await is_quota_exceeded(job.user_id, db, "storage_bytes"):
            return await _fail(db, job, run_id, now)
        if await is_quota_exceeded(job.user_id, db, "concurrent_jobs"):
            if input.last_chance:
                return await _skip(db, job, "concurrent_jobs", now)
            if job.schedule_waiting_since is None:
                job.schedule_waiting_since = now
            await db.commit()
            return Admission(verdict="waiting")

        run = JobRun(id=run_id, job_id=job.id, status="pending", lane="v2")
        db.add(run)
        job.last_run_at = now
        job.schedule_waiting_since = None
        await db.commit()
        return await _admitted(db, job, run)
