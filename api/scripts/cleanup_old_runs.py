import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import aliased

from app.core.ledger import ReleaseOutcome, release_run_objects
from app.core.minio import close_client, create_client
from app.models.job import Job
from app.models.job_runs import JobRun
from app.settings import settings

logger = structlog.get_logger()

BATCH_SIZE = 500
RETENTION_DAYS = int(os.environ.get("SCHEDULE_RUN_RETENTION_DAYS", "90"))
_FINISHED = ("completed", "failed", "cancelled")


async def _cleanup_loop(db: AsyncSession, minio, cutoff: datetime) -> None:
    total_deleted = 0
    label = "expired scheduled-run object on nightly cleanup"

    while True:
        newer = aliased(JobRun)
        rows = (
            await db.execute(
                select(JobRun, Job.user_id)
                .join(Job, JobRun.job_id == Job.id)
                .where(
                    JobRun.created_at < cutoff,
                    Job.schedule_status.is_not(None),
                    JobRun.status.in_(_FINISHED),
                    # A job's newest run is kept: a job with no runs drops out of every job read.
                    exists().where(
                        newer.job_id == JobRun.job_id, newer.created_at > JobRun.created_at
                    ),
                )
                .order_by(JobRun.created_at)
                .limit(BATCH_SIZE)
            )
        ).all()

        if not rows:
            break

        by_owner: dict[uuid.UUID, list[JobRun]] = {}
        for run, user_id in rows:
            by_owner.setdefault(user_id, []).append(run)

        outcome = ReleaseOutcome()
        for user_id, runs in by_owner.items():
            outcome.merge(await release_run_objects(db, minio, runs, user_id, label))
        # A run whose object is still on disk keeps its rows — retried next night.
        successful_ids = [str(run.id) for run, _ in rows if run.id not in outcome.failed_run_ids]

        if not successful_ids:
            # Every row in batch had a MinIO failure — break to avoid infinite loop
            logger.error(
                "cleanup: all minio deletes failed in batch, aborting",
                batch_size=len(rows),
            )
            break

        await db.flush()  # released ledger rows go before the raw DELETEs below
        await db.execute(
            text("DELETE FROM webhook_deliveries WHERE run_id = ANY(:ids)"),
            {"ids": successful_ids},
        )
        result = await db.execute(
            text("DELETE FROM job_runs WHERE id = ANY(:ids)"),
            {"ids": successful_ids},
        )
        await db.commit()

        total_deleted += result.rowcount
        logger.info(
            "cleanup: batch done",
            deleted=result.rowcount,
            skipped=len(rows) - len(successful_ids),
            objects_released=outcome.released,
            bytes_freed=outcome.freed_bytes,
            total_deleted=total_deleted,
        )


async def main() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    logger.info("cleanup: starting", retention_days=RETENTION_DAYS, cutoff=cutoff.isoformat())

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    minio = await create_client()
    try:
        async with AsyncSession(engine) as db:
            await _cleanup_loop(db, minio, cutoff)
    finally:
        await close_client(minio)
        await engine.dispose()

    logger.info("cleanup: finished")


if __name__ == "__main__":
    asyncio.run(main())
