"""An Approval block's three activities (ADR-016 §9b, §9c), on the workflow queue.

Each takes the run's lock and refuses a run that is gone, no longer running or asked to cancel
(ADR-009 15d's fallback for a Temporal cancel that was lost during a wait of up to days).
"""

import uuid

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core import approvals, webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import notify_run
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.workflows.activities.db import _cancel_requested, _lock_run
from app.workflows.pipeline import (
    APPROVAL_SIGNAL,
    REMIND_APPROVAL_ACTIVITY,
    REQUEST_APPROVAL_ACTIVITY,
    RESOLVE_APPROVAL_ACTIVITY,
    RUN_CANCELLED_ERROR,
    SUPERSEDE_APPROVAL_ACTIVITY,
    ApprovalOutcome,
    ApprovalRef,
    RequestApprovalInput,
    ResolveApprovalInput,
    SupersedeApprovalInput,
)

logger = structlog.get_logger()


async def _locked(db, input: ApprovalRef) -> tuple[PipelineRun, PipelineRunBlock]:
    run = await _lock_run(db, input.run_id)
    if run is None or run.status != "running" or _cancel_requested(run):
        raise ApplicationError(
            "the run was cancelled", type=RUN_CANCELLED_ERROR, non_retryable=True
        )
    block = await approvals.approval_block(db, run.id)
    if block is None or block.block_id != input.block_id:
        raise ApplicationError(
            f"no approval block {input.block_id!r} in run {input.run_id}", non_retryable=True
        )
    return run, block


async def _start_messages(db, run_id: uuid.UUID) -> None:
    await webhook_delivery.start_pending(activity.client, db, pipeline_run_id=run_id)


@activity.defn(name=REQUEST_APPROVAL_ACTIVITY)
async def request_approval(input: RequestApprovalInput) -> None:
    """Records the deadline and the change so far, and queues the request. A retry writes the same
    values and inserts no second row."""
    async with AsyncSessionLocal() as db:
        run, block = await _locked(db, input)
        block.approval_deadline = input.deadline
        if input.change_status is not None:
            run.change_status = input.change_status
        if input.change_summary is not None:
            run.change_summary = input.change_summary
        await db.flush()
        # Superseded between the waiting mirror and this request: nothing to ask.
        queued = block.approval_answer is None and await approvals.queue_message(
            db, run, block, approvals.REQUESTED_EVENT
        )
        await notify_run(db, run, block.block_id, block.status, block.status_reason)
        await db.commit()
        if queued:
            await _start_messages(db, run.id)


@activity.defn(name=REMIND_APPROVAL_ACTIVITY)
async def remind_approval(input: ApprovalRef) -> ApprovalOutcome:
    """Queues the reminder unless an answer is recorded; returns that answer (a lost signal)."""
    async with AsyncSessionLocal() as db:
        run, block = await _locked(db, input)
        if block.approval_answer is not None:
            return ApprovalOutcome(answer=block.approval_answer)
        queued = await approvals.queue_message(db, run, block, approvals.EXPIRING_EVENT)
        await db.commit()
        if queued:
            await _start_messages(db, run.id)
        return ApprovalOutcome()


@activity.defn(name=RESOLVE_APPROVAL_ACTIVITY)
async def resolve_approval(input: ResolveApprovalInput) -> ApprovalOutcome:
    """The recorded answer; with ``expire``, ``expired`` when none is (conditional on IS NULL, so
    an answer committed first stands)."""
    async with AsyncSessionLocal() as db:
        run, block = await _locked(db, input)
        if block.approval_answer is not None or not input.expire:
            return ApprovalOutcome(answer=block.approval_answer)
        if not await approvals.record_answer(db, block, "expired"):
            await db.refresh(block)
            return ApprovalOutcome(answer=block.approval_answer)
        queued = await approvals.queue_message(db, run, block, approvals.EXPIRED_EVENT)
        await db.commit()
        if queued:
            await _start_messages(db, run.id)
        return ApprovalOutcome(answer="expired")


async def _wake(run: PipelineRun) -> None:
    """Best effort: a lost wake-up is found at the reminder or the deadline."""
    try:
        await activity.client().get_workflow_handle(run.workflow_id).signal(APPROVAL_SIGNAL)
    except Exception as exc:
        logger.warning("pipeline_approval_signal_not_sent", run_id=str(run.id), error=str(exc))


@activity.defn(name=SUPERSEDE_APPROVAL_ACTIVITY)
async def supersede_approval(input: SupersedeApprovalInput) -> ApprovalOutcome:
    """ADR-016 §9e: a later check's content differs, so the waiting approval ends ``superseded``
    unless an answer is already recorded; then its workflow is woken. Returns the recorded
    answer — none when the run is no longer waiting (ended, cancelled or gone)."""
    by = uuid.UUID(input.superseded_by_run_id)
    async with AsyncSessionLocal() as db:
        run = await _lock_run(db, input.run_id)
        if run is None or run.status != "running" or _cancel_requested(run):
            return ApprovalOutcome()
        block = await approvals.approval_block(db, run.id)
        if block is None or block.status != "waiting":
            return ApprovalOutcome()
        if not await approvals.record_answer(db, block, "superseded"):
            await db.refresh(block)
            if block.approval_answer == "superseded" and block.superseded_by_run_id == by:
                # A retry after a lost reply: the first call's wake-up may not have been sent.
                await _wake(run)
            return ApprovalOutcome(answer=block.approval_answer)
        block.superseded_by_run_id = by
        await db.flush()
        queued = await approvals.queue_message(db, run, block, approvals.SUPERSEDED_EVENT)
        await notify_run(db, run, block.block_id, block.status, block.status_reason)
        await db.commit()
        if queued:
            await _start_messages(db, run.id)
        await _wake(run)
        return ApprovalOutcome(answer="superseded")
