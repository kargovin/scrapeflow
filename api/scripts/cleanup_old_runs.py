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
        # Join to jobs/batches to resolve user_id for storage quota decrement.
        rows = (
            await db.execute(
                text(
                    "SELECT jr.id, jr.result_path,"
                    " COALESCE(j.user_id::text, b.user_id::text) AS user_id"
                    " FROM job_runs jr"
                    " LEFT JOIN jobs j ON jr.job_id = j.id"
                    " LEFT JOIN batch_items bi ON jr.batch_item_id = bi.id"
                    " LEFT JOIN batches b ON bi.batch_id = b.id"
                    " WHERE jr.created_at < :cutoff ORDER BY jr.created_at LIMIT :limit"
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
                    # Stat before delete so we can decrement the quota accurately.
                    file_size = 0
                    try:
                        stat = await minio.stat_object(settings.minio_bucket, key)
                        file_size = stat.size or 0
                    except Exception:
                        pass  # best-effort; deletion still proceeds

                    try:
                        await minio.remove_object(settings.minio_bucket, key)
                    except Exception:
                        logger.exception(
                            "cleanup: minio delete failed, skipping db delete",
                            run_id=str(row.id),
                            key=key,
                        )
                        continue  # leave DB row intact — retry next night

                    # Decrement the owning user's storage quota.
                    if row.user_id and file_size > 0:
                        try:
                            await db.execute(
                                text("""
                                    INSERT INTO user_quotas (user_id, storage_bytes_used, updated_at)
                                    VALUES (:user_id, 0, NOW())
                                    ON CONFLICT (user_id) DO UPDATE
                                    SET storage_bytes_used = GREATEST(
                                            0, user_quotas.storage_bytes_used - :size
                                        ),
                                        updated_at = NOW()
                                """),
                                {"user_id": row.user_id, "size": file_size},
                            )
                        except Exception:
                            logger.exception(
                                "cleanup: quota decrement failed",
                                run_id=str(row.id),
                                user_id=row.user_id,
                            )

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
