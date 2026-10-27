"""A batch's counters and its ``batch_status`` notify, for the v2 lane and the cancel route.

The counters are recounted from the items, never incremented (ADR-009 11d): the v2 mirrors run at
least once. Lock the batch row before recounting — the count is read after the lock, so it sees
an item another mirror committed while this one waited.
"""

import json

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.batch import Batch, BatchItem

TERMINAL = frozenset({"completed", "partial_failure", "cancelled"})


async def lock(db: AsyncSession, batch_id) -> Batch | None:
    return await db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def recount(db: AsyncSession, batch: Batch) -> None:
    await db.flush()
    completed, failed = (
        await db.execute(
            select(
                func.count().filter(BatchItem.status == "completed"),
                func.count().filter(BatchItem.status == "failed"),
            ).where(BatchItem.batch_id == batch.id)
        )
    ).one()
    batch.completed, batch.failed = completed, failed


async def notify(
    db: AsyncSession, batch: Batch, item_url: str | None = None, item_status: str | None = None
) -> None:
    payload = {
        "batch_id": str(batch.id),
        "completed": batch.completed,
        "failed": batch.failed,
        "total": batch.total,
        "status": batch.status,
    }
    if item_url is not None:
        payload |= {"item_url": item_url, "item_status": item_status}
    await db.execute(text("SELECT pg_notify('batch_status', :p)"), {"p": json.dumps(payload)})
