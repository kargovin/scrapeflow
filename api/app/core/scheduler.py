"""Scheduler background task.

scheduler_loop polls every 60 seconds and:
  1. Dispatches any cron jobs whose next_run_at <= now() (FOR UPDATE SKIP LOCKED)
  2. Re-publishes any job_runs stuck in 'pending' for > 10 minutes (stale-pending recovery)

DB is committed before NATS publish (ADR-001): a NATS failure after commit leaves a
pending JobRun that the stale-pending recovery will re-publish on the next cycle.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import structlog
from croniter import croniter
from nats.js import JetStreamContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.models.job import Job
from app.models.job_runs import JobRun

logger = structlog.get_logger()


async def scheduler_loop(
    db_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
) -> None:
    """Background task: dispatch due cron jobs and recover stale pending runs."""
    while True:
        await asyncio.sleep(60)  # sleep at top — no immediate trigger on startup
        try:
            await _dispatch_due_jobs(db_factory, js)
            await _recover_stale_pending(db_factory, js)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler_loop: unhandled error, continuing")


async def _dispatch_due_jobs(
    db_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
) -> None:
    """Create a JobRun and publish to NATS for every cron job that is now due."""
    async with db_factory() as db:
        stmt = (
            select(Job)
            .where(
                Job.schedule_cron.is_not(None),
                Job.schedule_status == "active",
                Job.next_run_at <= datetime.now(UTC),
            )
            .with_for_update(skip_locked=True)
        )
        jobs = (await db.execute(stmt)).scalars().all()

        for job in jobs:
            run = JobRun(job_id=job.id, status="pending")
            db.add(run)
            await db.flush()  # populate run.id before publish

            now = datetime.now(UTC)
            job.last_run_at = now
            # Use stored next_run_at as croniter base — prevents schedule drift from poll jitter.
            # croniter returns a naive datetime; attach UTC explicitly.
            job.next_run_at = (
                croniter(job.schedule_cron, job.next_run_at).get_next(datetime).replace(tzinfo=UTC)
            )

            await db.commit()  # commit per job — releases row lock immediately

            payload: dict = {
                "job_id": str(job.id),
                "run_id": str(run.id),
                "url": job.url,
                "output_format": job.output_format.value,
            }
            try:
                if job.engine == "playwright":
                    payload["playwright_options"] = job.playwright_options  # already a dict (JSONB)
                    await js.publish(NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT, json.dumps(payload).encode())
                else:
                    await js.publish(NATS_JOBS_RUN_HTTP_SUBJECT, json.dumps(payload).encode())
            except Exception:
                logger.exception(
                    "scheduler: NATS publish failed after DB commit — stale-pending recovery will retry",
                    job_id=str(job.id),
                    run_id=str(run.id),
                )
                continue

            logger.info(
                "scheduler: dispatched job",
                job_id=str(job.id),
                run_id=str(run.id),
                engine=job.engine,
            )


async def _recover_stale_pending(
    db_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
) -> None:
    """Re-publish NATS messages for job_runs stuck in pending > 10 minutes.

    This catches the crash-after-commit-before-publish failure mode from _dispatch_due_jobs.
    No new JobRun is created — the existing run is re-published as-is.
    """
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=10)

    async with db_factory() as db:
        stmt = (
            select(JobRun)
            .where(
                JobRun.status == "pending",
                JobRun.created_at < stale_cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        stale_runs = (await db.execute(stmt)).scalars().all()

        for run in stale_runs:
            job = await db.get(Job, run.job_id)
            if job is None:
                continue  # orphaned run — should not happen with CASCADE deletes

            payload: dict = {
                "job_id": str(job.id),
                "run_id": str(run.id),
                "url": job.url,
                "output_format": job.output_format.value,
            }
            try:
                if job.engine == "playwright":
                    payload["playwright_options"] = job.playwright_options
                    await js.publish(NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT, json.dumps(payload).encode())
                else:
                    await js.publish(NATS_JOBS_RUN_HTTP_SUBJECT, json.dumps(payload).encode())
            except Exception:
                logger.exception(
                    "scheduler: NATS publish failed for stale-pending recovery — will retry next cycle",
                    run_id=str(run.id),
                    job_id=str(job.id),
                )
                continue

            logger.info(
                "scheduler: re-published stale pending run",
                run_id=str(run.id),
                job_id=str(job.id),
            )
        # No DB write — status stays pending; the worker result will advance it
