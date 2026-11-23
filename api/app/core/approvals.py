"""An Approval block's record and its messages (ADR-016 §9b, §9c).

The block row is the record: the answer is written once, conditionally on ``approval_answer IS NULL``,
by whichever writer commits first — the owner's answer, the expiry, or a supersede. Every message
(request, reminder, expired, superseded) is a ``webhook_deliveries`` row keyed
``(pipeline_run_id, block_id, event)`` to the block's own destination; none to a monitor's (§8).
An expired check's monitor notice is the monitor's own (``monitor_checks``, ADR-017).
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.models.destination import Destination
from app.models.pipeline import PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.catalog import APPROVAL
from app.pipelines.definition import ApprovalBlock, parse_definition
from app.settings import settings
from app.workflows.pipeline import APPROVAL_SIGNAL

logger = structlog.get_logger()

REQUESTED_EVENT = "pipeline.approval_requested"
EXPIRING_EVENT = "pipeline.approval_expiring"
EXPIRED_EVENT = "pipeline.approval_expired"
SUPERSEDED_EVENT = "pipeline.approval_superseded"
APPROVAL_EVENTS = (REQUESTED_EVENT, EXPIRING_EVENT, EXPIRED_EVENT, SUPERSEDED_EVENT)

DELIVERIES_NOTE = (
    "Already delivered by this run. A reject keeps them; they are removed only if this run later "
    "fails."
)


def run_url(run: PipelineRun) -> str:
    return f"{settings.public_base_url}/app/dashboard/pipelines/{run.pipeline_id}/runs/{run.id}"


async def approval_block(db: AsyncSession, run_id: uuid.UUID) -> PipelineRunBlock | None:
    """The run's Approval block row (at most one per pipeline, ADR-016 §9a)."""
    return await db.scalar(
        select(PipelineRunBlock).where(
            PipelineRunBlock.pipeline_run_id == run_id, PipelineRunBlock.type == APPROVAL.name
        )
    )


def approval_state(block: PipelineRunBlock) -> str:
    """What the SPA and a refused answer show: the recorded answer, else where the block is."""
    if block.approval_answer is not None:
        return block.approval_answer
    if block.status == "waiting" and block.approval_deadline is not None:
        return "waiting"
    if block.status in ("pending", "waiting"):
        return "not_requested"
    # Cancelled, failed or skipped without an answer.
    return "closed"


async def deliveries(db: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """What the run's sinks have delivered so far (ADR-014 §5a records), chain order."""
    rows = await db.execute(
        select(PipelineRunBlock.block_id, PipelineRunBlock.delivery)
        .where(PipelineRunBlock.pipeline_run_id == run_id)
        .order_by(PipelineRunBlock.position)
    )
    # ⚠️ Not SQL IS NOT NULL: the mirror writes JSON null on every completed block.
    return [
        {"block_id": block_id, "kind": d["kind"], "location": d["location"]}
        for block_id, d in rows
        if d is not None
    ]


async def message_payload(
    db: AsyncSession, run: PipelineRun, block: PipelineRunBlock, event: str
) -> dict[str, Any]:
    delivered = await deliveries(db, run.id)
    payload: dict[str, Any] = {
        "event": event,
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "pipeline_run_id": str(run.id),
        "block_id": block.block_id,
        "monitor_id": str(run.monitor_id) if run.monitor_id else None,
        "deadline": block.approval_deadline.isoformat() if block.approval_deadline else None,
        "change_status": run.change_status,
        "change_summary": run.change_summary,
        "deliveries": delivered,
        "run_url": run_url(run),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if delivered:
        payload["deliveries_note"] = DELIVERIES_NOTE
    if block.approval_answer is not None:
        payload["answer"] = block.approval_answer
    if block.superseded_by_run_id is not None:
        payload["superseded_by_run_id"] = str(block.superseded_by_run_id)
    return payload


async def _destination_id(db: AsyncSession, run: PipelineRun, block_id: str) -> uuid.UUID | None:
    version = await db.get(PipelineVersion, run.pipeline_version_id)
    assert version is not None  # FK
    parsed, _ = parse_definition(version.definition)
    for block in parsed.blocks:
        if isinstance(block, ApprovalBlock) and block.id == block_id:
            return block.config.destination_id
    return None


async def queue_message(
    db: AsyncSession, run: PipelineRun, block: PipelineRunBlock, event: str
) -> bool:
    """Adds the message row to the caller's transaction when the block names a destination;
    ON CONFLICT DO NOTHING, so a retried writer inserts it once. The caller starts it after commit.
    """
    destination_id = await _destination_id(db, run, block.block_id)
    if destination_id is None:
        return False
    # FOR KEY SHARE holds it until commit, so the insert's foreign key cannot fail. Deleted
    # outside the API: the row is written with none and ends "destination deleted".
    destination = await db.scalar(
        select(Destination)
        .where(Destination.id == destination_id)
        .with_for_update(read=True, key_share=True)
    )
    await db.execute(
        pg_insert(WebhookDelivery)
        .values(
            pipeline_run_id=run.id,
            block_id=block.block_id,
            event=event,
            kind="email" if destination is not None and destination.kind == "smtp" else "webhook",
            # Its URL or mail settings are read at each attempt (ADR-015 §2b).
            destination_id=destination.id if destination is not None else None,
            payload=await message_payload(db, run, block, event),
            status="pending",
            next_attempt_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(
            index_elements=["pipeline_run_id", "block_id", "event"],
            index_where=WebhookDelivery.pipeline_run_id.isnot(None),
        )
    )
    return True


async def wake(get_client: Callable[[], Client], run_id: uuid.UUID, workflow_id: str) -> bool:
    """Tells the run's workflow an answer may be recorded. Best effort, after the commit: a lost
    wake-up is found at the reminder, the deadline or the monitor's next firing."""
    try:
        await get_client().get_workflow_handle(workflow_id).signal(APPROVAL_SIGNAL)
    except Exception as exc:
        logger.warning("pipeline_approval_signal_not_sent", run_id=str(run_id), error=str(exc))
        return False
    return True


async def record_answer(
    db: AsyncSession,
    block: PipelineRunBlock,
    answer: str,
    *,
    answered_by: uuid.UUID | None = None,
    open_until_deadline: bool = False,
) -> bool:
    """The one conditional write of an answer; the caller holds the run's lock. False: another
    answer is already recorded (or, for the owner's, the block is not waiting or the deadline
    has passed)."""
    conditions = [PipelineRunBlock.id == block.id, PipelineRunBlock.approval_answer.is_(None)]
    if open_until_deadline:
        conditions += [
            PipelineRunBlock.status == "waiting",
            # Not now(): that is the transaction's start, before the wait for the run's lock.
            PipelineRunBlock.approval_deadline > func.clock_timestamp(),
        ]
    values: dict[str, Any] = {"approval_answer": answer}
    if answered_by is not None:
        values |= {"answered_at": datetime.now(UTC), "answered_by": answered_by}
    result = await db.execute(update(PipelineRunBlock).where(*conditions).values(**values))
    if result.rowcount != 1:
        return False
    await db.refresh(block)
    return True
