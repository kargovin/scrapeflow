"""Batch-lane app-DB activity — what ``result_consumer.py`` does when a batch's last item lands."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio import activity

from app.core import batch_progress
from app.core.db import AsyncSessionLocal
from app.core.webhooks import create_batch_webhook_delivery
from app.models.batch import BatchItem
from app.models.job_runs import JobRun
from app.workflows.activities.job_runs import ACTIVE, GONE
from app.workflows.batch import FINISH_BATCH_ACTIVITY, FinishBatchInput

# A run still active once every child has ended: its child never started, or was terminated.
NO_OUTCOME = "internal error: the run's workflow ended without an outcome"


@activity.defn(name=FINISH_BATCH_ACTIVITY)
async def finish_batch(input: FinishBatchInput) -> str:
    """Closes the batch and writes its webhook row in one transaction; returns its status.

    A run still active is cancelled on a cancel (the workflow's, or the API's — the batch already
    says ``cancelled``), else failed. A batch already closed is left as it is: a retried activity.
    """
    batch_id = uuid.UUID(input.batch_id)
    async with AsyncSessionLocal() as db:
        # Lock order run → batch → item, as the mirrors and the cancel route take them.
        active = (
            await db.scalars(
                select(JobRun)
                .join(BatchItem, BatchItem.id == JobRun.batch_item_id)
                .where(BatchItem.batch_id == batch_id, JobRun.status.in_(ACTIVE))
                .order_by(JobRun.id)
                .with_for_update(of=JobRun)
            )
        ).all()
        batch = await batch_progress.lock(db, batch_id)
        if batch is None:
            return GONE
        if batch.status in ("completed", "partial_failure"):
            return batch.status

        cancelled = input.cancelled or batch.status == "cancelled"
        now = datetime.now(UTC)
        for run in active:
            run.status = "cancelled" if cancelled else "failed"
            run.error = None if cancelled else NO_OUTCOME
            run.completed_at = now
            item = await db.get(BatchItem, run.batch_item_id)
            item.status, item.error, item.completed_at = run.status, run.error, now
        await batch_progress.recount(db, batch)

        if cancelled:
            batch.status = "cancelled"
        else:
            batch.status = "completed" if batch.completed == batch.total else "partial_failure"
            batch.completed_at = now
            if batch.webhook_url:
                # Any run of the batch keys the row's dedup; take the first item's.
                first_run = await db.scalar(
                    select(JobRun.id)
                    .join(BatchItem, BatchItem.id == JobRun.batch_item_id)
                    .where(BatchItem.batch_id == batch_id)
                    .order_by(BatchItem.created_at, JobRun.id)
                    .limit(1)
                )
                await create_batch_webhook_delivery(db, batch, first_run, event="batch.completed")
        await batch_progress.notify(db, batch)
        await db.commit()
        return batch.status
