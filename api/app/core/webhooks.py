"""Webhook delivery helper.

create_webhook_delivery inserts a pending WebhookDelivery row into the DB.
It does NOT commit — the caller owns the transaction so the delivery row
and any job_run status update land atomically in the same commit.

The actual HTTP POST is handled by the webhook_delivery_loop (Step 21).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.diff import DiffResult
from app.models.batch import Batch
from app.models.job import Job
from app.models.webhook_delivery import WebhookDelivery


async def create_webhook_delivery(
    db: AsyncSession,
    job: Job,
    run_id: uuid.UUID,
    event: str,
    minio_path: str | None,
    diff: DiffResult | None = None,
    error: str | None = None,
) -> None:
    """Add a pending WebhookDelivery row to the session.

    event      — "job.completed" or "job.failed"
    minio_path — history/ path of the result; None for failed events
    diff       — DiffResult from compute_text_diff / compute_json_diff; None on first run
    error      — error message; only included in the payload when event="job.failed"

    Uses ON CONFLICT DO NOTHING against idx_webhook_deliveries_dedup so a redelivered
    NATS message that reaches this call site cannot insert a duplicate delivery row.
    """
    payload: dict = {
        "event": event,
        "job_id": str(job.id),
        "run_id": str(run_id),
        "result_path": minio_path,
        "diff_detected": diff.detected if diff is not None else None,
        "diff_summary": diff.summary if diff is not None else None,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if error is not None:
        payload["error"] = error

    await db.execute(
        pg_insert(WebhookDelivery)
        .values(
            job_id=job.id,
            run_id=run_id,
            event=event,
            webhook_url=job.webhook_url,
            payload=payload,
            status="pending",
            next_attempt_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(
            index_elements=["run_id", "event"],
            index_where=WebhookDelivery.run_id.isnot(None),
        )
    )


async def create_batch_webhook_delivery(
    db: AsyncSession,
    batch: Batch,
    run_id: uuid.UUID,
    event: str,
) -> None:
    """Add a pending WebhookDelivery row for a batch event. Caller owns the transaction.

    event — "batch.completed"
    run_id — the triggering item's run; used for the job_runs FK.

    Uses ON CONFLICT DO NOTHING — same dedup guarantee as create_webhook_delivery.
    """
    payload: dict = {
        "event": event,
        "batch_id": str(batch.id),
        "total": batch.total,
        "completed": batch.completed,
        "failed": batch.failed,
        "status": batch.status,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    await db.execute(
        pg_insert(WebhookDelivery)
        .values(
            batch_id=batch.id,
            run_id=run_id,
            event=event,
            webhook_url=batch.webhook_url,
            payload=payload,
            status="pending",
            next_attempt_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(
            index_elements=["run_id", "event"],
            index_where=WebhookDelivery.run_id.isnot(None),
        )
    )
