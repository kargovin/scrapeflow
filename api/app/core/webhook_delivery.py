"""Starting job, batch and crawl webhook deliveries (F.4) — one ``WebhookDeliveryWorkflow`` per row.

Whoever commits a ``webhook_deliveries`` row starts its delivery right after, best effort. A start
that is lost (a crash after the commit, Temporal down) is picked up by the ``webhook-sweep``
Schedule within ``SWEEP_EVERY``: the row, not the start, is what guarantees delivery.
"""

import uuid
from collections.abc import Callable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from app.models.webhook_delivery import WebhookDelivery
from app.workflows.queues import WORKFLOW_QUEUE
from app.workflows.webhook_delivery import (
    SWEEP_EVERY,
    SWEEP_SCHEDULE_ID,
    DeliveryRef,
    WebhookDeliveryWorkflow,
    WebhookSweepWorkflow,
    workflow_id,
)

logger = structlog.get_logger()


async def start(client: Client, delivery_ids: list[uuid.UUID]) -> None:
    """Starts each row's delivery. One already running for a row is left as it is; a closed one
    (delivered, given up, or failed) does not block a new start — the row says what is owed."""
    for delivery_id in delivery_ids:
        await client.start_workflow(
            WebhookDeliveryWorkflow.run,
            DeliveryRef(delivery_id=str(delivery_id)),
            id=workflow_id(delivery_id),
            task_queue=WORKFLOW_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )


async def pending(
    db: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    crawl_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    stmt = select(WebhookDelivery.id).where(WebhookDelivery.status == "pending")
    if run_id is not None:
        stmt = stmt.where(WebhookDelivery.run_id == run_id)
    if batch_id is not None:
        stmt = stmt.where(WebhookDelivery.batch_id == batch_id)
    if crawl_id is not None:
        stmt = stmt.where(WebhookDelivery.crawl_id == crawl_id)
    return list((await db.scalars(stmt)).all())


async def start_pending(
    get_client: Callable[[], Client], db: AsyncSession, **owner: uuid.UUID
) -> None:
    """Best effort, after the commit that wrote the row(s): the sweep covers a failure.

    ``get_client`` is ``activity.client`` in an activity — called in here, so a caller outside
    one (a test calling the activity function) only logs."""
    try:
        await start(get_client(), await pending(db, **owner))
    except Exception as exc:
        logger.warning(
            "webhook_delivery_start_failed",
            **{k: str(v) for k, v in owner.items()},
            error=str(exc),
        )


async def ensure_sweep(client: Client) -> None:
    """Creates the ``webhook-sweep`` Schedule, or brings an existing one up to date."""
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            WebhookSweepWorkflow.run, id="webhook-sweep-run", task_queue=WORKFLOW_QUEUE
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=SWEEP_EVERY)]),
        # A sweep missed while the server was down is made up by the next one.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=SWEEP_EVERY),
    )
    try:
        await client.create_schedule(SWEEP_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        await client.get_schedule_handle(SWEEP_SCHEDULE_ID).update(
            lambda _: ScheduleUpdate(schedule=schedule)
        )
