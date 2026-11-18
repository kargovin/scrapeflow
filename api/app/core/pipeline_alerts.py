"""A pipeline run's failure alerts (ADR-014 §8) — one ``webhook_deliveries`` row per Webhook block
whose ``events`` include ``pipeline.failed``, delivered by ``WebhookDeliveryWorkflow``.

Both writers of a ``failed`` run call ``queue_failure_alerts`` in the transaction that sets it (the
mirror activity and the trigger's could-not-start branch), so "run failed" and "alert queued" are
true together or not at all. Each then starts the rows after its commit, best effort.
"""

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import PipelineRun, PipelineVersion
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.definition import WebhookBlock, parse_definition

FAILED_EVENT = "pipeline.failed"


def failed_payload(run: PipelineRun, block_id: str) -> dict:
    # pipeline.result's fields (activities/webhook.py), plus what broke and what was undone.
    return {
        "event": FAILED_EVENT,
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "pipeline_run_id": str(run.id),
        "block_id": block_id,
        "failed_block_id": run.failed_block_id,
        "error": run.error,
        "result_path": None,
        "diff_detected": False,
        "diff_summary": None,
        # Filled by rollback (PRD-017); nothing is delivered to undo before then.
        "rollback": {"undone": [], "left_in_place": [], "kept": []},
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def queue_failure_alerts(db: AsyncSession, run: PipelineRun) -> int:
    """Adds the run's alert rows to the caller's transaction; returns how many blocks send one.

    ON CONFLICT DO NOTHING: a retried writer inserts nothing twice."""
    version = await db.get(PipelineVersion, run.pipeline_version_id)
    assert version is not None
    parsed, _ = parse_definition(version.definition)
    senders = [
        b for b in parsed.blocks if isinstance(b, WebhookBlock) and FAILED_EVENT in b.config.events
    ]
    for block in senders:
        await db.execute(
            pg_insert(WebhookDelivery)
            .values(
                pipeline_run_id=run.id,
                block_id=block.id,
                event=FAILED_EVENT,
                webhook_url=str(block.config.url),
                payload=failed_payload(run, block.id),
                status="pending",
                next_attempt_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(
                index_elements=["pipeline_run_id", "block_id", "event"],
                index_where=WebhookDelivery.pipeline_run_id.isnot(None),
            )
        )
    return len(senders)


async def alert_secret(db: AsyncSession, delivery: WebhookDelivery) -> str | None:
    """The sending block's stored secret (credentials key ciphertext), from the pinned version."""
    run = await db.get(PipelineRun, delivery.pipeline_run_id)
    if run is None:
        return None
    version = await db.get(PipelineVersion, run.pipeline_version_id)
    assert version is not None
    _, secrets = parse_definition(version.definition)
    return secrets.get(delivery.block_id or "", {}).get("secret")
