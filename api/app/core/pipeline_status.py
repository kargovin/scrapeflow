"""The ``pipeline_status`` notify channel — written by the mirror activities and the API's cancel,
read by ``JobNotifier`` for the run WebSocket (ADR-009 §11)."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import PipelineRun

PIPELINE_STATUS_CHANNEL = "pipeline_status"


async def notify_run(
    db: AsyncSession,
    run: PipelineRun,
    block_id: str | None = None,
    block_status: str | None = None,
) -> None:
    """Queues a notification in the caller's transaction; delivered on its commit."""
    # Identifiers and absolute state only — never error text (11d: 8000-byte cap, at-least-once).
    payload = {
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "status": run.status,
        "cancel_requested": run.cancel_requested_at is not None,
        "block_id": block_id,
        "block_status": block_status,
    }
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": PIPELINE_STATUS_CHANNEL, "payload": json.dumps(payload)},
    )
