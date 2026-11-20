"""A pipeline run's failure alerts (ADR-014 §8) — one ``webhook_deliveries`` row per Webhook block
whose ``events`` include ``pipeline.failed``, delivered by ``WebhookDeliveryWorkflow``.

Both writers of a ``failed`` run call ``queue_failure_alerts`` in the transaction that sets it (the
mirror activity and the trigger's could-not-start branch), so "run failed" and "alert queued" are
true together or not at all. Each then starts the rows after its commit, best effort.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.definition import WebhookBlock, parse_definition

FAILED_EVENT = "pipeline.failed"

# The payload's rollback list for each undo outcome a delivered block can end with (ADR-014 §8c).
_ROLLBACK_LISTS = {"undone": "undone", "undo_failed": "left_in_place", "kept": "kept"}


def _no_rollback() -> dict[str, list]:
    return {"undone": [], "left_in_place": [], "kept": []}


async def rollback_lists(db: AsyncSession, run_id) -> dict[str, list]:
    """Each delivered block by how its rollback ended: ``{block_id, kind, location}``, chain order."""
    lists = _no_rollback()
    rows = await db.execute(
        select(PipelineRunBlock.block_id, PipelineRunBlock.delivery, PipelineRunBlock.undo_status)
        .where(
            PipelineRunBlock.pipeline_run_id == run_id,
            PipelineRunBlock.delivery.is_not(None),
            PipelineRunBlock.undo_status.in_(_ROLLBACK_LISTS),
        )
        .order_by(PipelineRunBlock.position)
    )
    for block_id, delivery, undo_status in rows:
        lists[_ROLLBACK_LISTS[undo_status]].append(
            {"block_id": block_id, "kind": delivery["kind"], "location": delivery["location"]}
        )
    return lists


def failed_payload(run: PipelineRun, block_id: str, rollback: dict[str, Any] | None = None) -> dict:
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
        "rollback": rollback if rollback is not None else _no_rollback(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def queue_failure_alerts(db: AsyncSession, run: PipelineRun) -> int:
    """Adds the run's alert rows to the caller's transaction; returns how many blocks send one.

    ON CONFLICT DO NOTHING: a retried writer inserts nothing twice. Written after rollback, so
    the rows carry how it ended."""
    version = await db.get(PipelineVersion, run.pipeline_version_id)
    assert version is not None
    parsed, _ = parse_definition(version.definition)
    senders = [
        b for b in parsed.blocks if isinstance(b, WebhookBlock) and FAILED_EVENT in b.config.events
    ]
    rollback = await rollback_lists(db, run.id) if senders else None
    for block in senders:
        await db.execute(
            pg_insert(WebhookDelivery)
            .values(
                pipeline_run_id=run.id,
                block_id=block.id,
                event=FAILED_EVENT,
                # A destination's URL is read at each attempt and never stored (ADR-014 §8b).
                webhook_url=str(block.config.url) if block.config.url is not None else None,
                destination_id=block.config.destination_id,
                payload=failed_payload(run, block.id, rollback),
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
