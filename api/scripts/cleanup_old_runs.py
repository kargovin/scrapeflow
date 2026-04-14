import asyncio
import os
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.minio import close_client, create_client
from app.settings import settings

logger = structlog.get_logger()

BATCH_SIZE = 500
RETENTION_DAYS = int(os.environ.get("SCHEDULE_RUN_RETENTION_DAYS", "90"))


async def _cleanup_loop(db: AsyncSession, minio, cutoff: datetime) -> None:
    total_deleted = 0

    while True:
        rows = (
            await db.execute(
                text(
                    "SELECT id, result_path FROM job_runs"
                    " WHERE created_at < :cutoff ORDER BY created_at LIMIT :limit"
                ),
                {"cutoff": cutoff, "limit": BATCH_SIZE},
            )
        ).fetchall()

        if not rows:
            break

        successful_ids: list[str] = []
        for row in rows:
            if row.result_path is not None:
                _, _, key = row.result_path.partition("/")
                if key.startswith("history/"):
                    try:
                        await minio.remove_object(settings.minio_bucket, key)
                    except Exception:
                        logger.exception(
                            "cleanup: minio delete failed, skipping db delete",
                            run_id=str(row.id),
                            key=key,
                        )
                        continue  # leave DB row intact — retry next night
            successful_ids.append(str(row.id))

        if not successful_ids:
            # Every row in batch had a MinIO failure — break to avoid infinite loop
            logger.error(
                "cleanup: all minio deletes failed in batch, aborting",
                batch_size=len(rows),
            )
            break

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
