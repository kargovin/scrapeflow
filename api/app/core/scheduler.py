"""Scheduler background task.

scheduler_loop polls every 60 seconds and:
  1. Dispatches any cron jobs whose next_run_at <= now() (FOR UPDATE SKIP LOCKED)
  2. Re-publishes any job_runs stuck in 'pending' beyond the configured threshold (stale-pending recovery)

DB is committed before NATS publish (ADR-001): a NATS failure after commit leaves a
pending JobRun that the stale-pending recovery will re-publish on the next cycle.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from croniter import croniter
from nats.js import JetStreamContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from app.core.credentials import resolve_credentials
from app.core.dispatch import (
    TemporalClient,
    build_batch_scrape_message,
    build_scrape_message,
    dispatch_run,
    fail_unstarted_run,
    nats_subject,
)
from app.core.quota import is_quota_exceeded, lock_user
from app.models.batch import Batch, BatchItem
from app.models.job import Job
from app.models.job_runs import JobRun
from app.settings import settings

logger = structlog.get_logger()


async def _no_temporal() -> Client:
    raise RuntimeError("no Temporal client")


async def scheduler_loop(
    db_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
    temporal: TemporalClient,
) -> None:
    """Background task: dispatch due cron jobs and recover stale pending runs."""
    while True:
        await asyncio.sleep(60)  # sleep at top — no immediate trigger on startup
        try:
            await _dispatch_due_jobs(db_factory, js, temporal)
            await _recover_stale_pending(db_factory, js)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler_loop: unhandled error, continuing")


async def _dispatch_due_jobs(
    db_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
    temporal: TemporalClient = _no_temporal,
) -> None:
    """Create a JobRun for every cron job that is now due and send it to its lane's engine."""
    async with db_factory() as db:
        stmt = (
            select(Job)
            .where(
                Job.schedule_cron.is_not(None),
                Job.schedule_status == "active",
                # A v2 schedule is a Temporal Schedule's; firing it here too would run it twice.
                Job.schedule_lane == "v1",
                Job.next_run_at <= datetime.now(UTC),
            )
            .with_for_update(skip_locked=True)
        )
        jobs = (await db.execute(stmt)).scalars().all()

        for job in jobs:
            await lock_user(job.user_id, db)
            if await is_quota_exceeded(job.user_id, db, "monthly_runs"):
                logger.warning(
                    "scheduler: skipping dispatch — monthly_runs quota exceeded",
                    job_id=str(job.id),
                    user_id=str(job.user_id),
                )
                continue
            if await is_quota_exceeded(job.user_id, db, "concurrent_jobs"):
                logger.warning(
                    "scheduler: skipping dispatch — concurrent_jobs quota exceeded",
                    job_id=str(job.id),
                    user_id=str(job.user_id),
                )
                continue

            run = JobRun(job_id=job.id, status="pending", lane=settings.job_lane)
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

            credentials = await resolve_credentials(job, db)
            try:
                await dispatch_run(job, run, credentials, js, temporal)
            except Exception as exc:
                if run.lane == "v1":
                    logger.exception(
                        "scheduler: NATS publish failed after DB commit — stale-pending recovery will retry",
                        job_id=str(job.id),
                        run_id=str(run.id),
                    )
                    continue
                logger.exception(
                    "scheduler: workflow start failed — run failed",
                    job_id=str(job.id),
                    run_id=str(run.id),
                )
                await fail_unstarted_run(db, job, run, exc)
                continue

            logger.info(
                "scheduler: dispatched job",
                job_id=str(job.id),
                run_id=str(run.id),
                engine=job.engine,
                lane=run.lane,
            )


async def _recover_stale_pending(
    db_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
) -> None:
    """Re-publish NATS messages for job_runs stuck in pending longer than the configured threshold.

    This catches the crash-after-commit-before-publish failure mode from _dispatch_due_jobs
    and create_batch. No new JobRun is created — the existing run is re-published as-is.

    A run belongs to exactly one lane (job_runs CHECK: one of job_id / batch_item_id is set)
    and each lane rebuilds its message from a different parent, so the loop resolves the
    message per lane, then publishes through one shared tail. Every branch that skips a run
    logs why — this is the only recovery path for a lost dispatch, and a silent skip here is
    how batch runs went unrecovered from the day batch shipped (BUG-011).
    """
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=settings.stale_pending_threshold_minutes)

    async with db_factory() as db:
        stmt = (
            select(JobRun)
            .where(
                JobRun.status == "pending",
                JobRun.created_at < stale_cutoff,
                # A v2 run is Temporal's: re-publishing it to NATS would run it twice.
                JobRun.lane == "v1",
            )
            .with_for_update(skip_locked=True)
        )
        stale_runs = (await db.execute(stmt)).scalars().all()

        for run in stale_runs:
            if run.job_id is not None:
                job = await db.get(Job, run.job_id)
                if job is None:
                    # Unreachable: job_runs.job_id is ON DELETE CASCADE. Logged, not silent.
                    logger.warning(
                        "scheduler: stale pending run has no job — skipping",
                        run_id=str(run.id),
                        job_id=str(run.job_id),
                    )
                    continue
                credentials = await resolve_credentials(job, db)
                message = build_scrape_message(job, run, credentials)
                engine = job.engine
                log_ctx = {"job_id": str(job.id)}
            elif run.batch_item_id is not None:
                item = await db.get(BatchItem, run.batch_item_id)
                if item is None:
                    # Unreachable: job_runs.batch_item_id is ON DELETE CASCADE. Logged, not silent.
                    logger.warning(
                        "scheduler: stale pending run has no batch item — skipping",
                        run_id=str(run.id),
                        batch_item_id=str(run.batch_item_id),
                    )
                    continue
                batch = await db.get(Batch, item.batch_id)
                if batch is None:
                    # Unreachable: batch_items.batch_id is ON DELETE CASCADE. Logged, not silent.
                    logger.warning(
                        "scheduler: stale pending run has no batch — skipping",
                        run_id=str(run.id),
                        batch_item_id=str(item.id),
                        batch_id=str(item.batch_id),
                    )
                    continue
                message = build_batch_scrape_message(batch, item, run)
                engine = batch.engine
                log_ctx = {"batch_id": str(batch.id), "batch_item_id": str(item.id)}
            else:
                # Unreachable: the job_runs CHECK requires exactly one FK. Logged, not silent.
                logger.warning(
                    "scheduler: stale pending run has neither job_id nor batch_item_id — skipping",
                    run_id=str(run.id),
                )
                continue

            try:
                await js.publish(nats_subject(engine), message.to_nats_bytes())
            except Exception:
                logger.exception(
                    "scheduler: NATS publish failed for stale-pending recovery — will retry next cycle",
                    run_id=str(run.id),
                    **log_ctx,
                )
                continue

            logger.info(
                "scheduler: re-published stale pending run",
                run_id=str(run.id),
                engine=engine,
                **log_ctx,
            )
        # No DB write — status stays pending; the worker result will advance it
