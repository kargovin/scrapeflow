"""An Approval block's three activities (ADR-016 §9b, §9c), on the workflow queue.

Each takes the run's lock and refuses a run that is gone, no longer running or asked to cancel
(ADR-009 15d's fallback for a Temporal cancel that was lost during a wait of up to days).
"""

import uuid

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core import approvals, webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import notify_run
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.workflows.activities.db import _cancel_requested, _lock_run
from app.workflows.pipeline import (
    REMIND_APPROVAL_ACTIVITY,
    REQUEST_APPROVAL_ACTIVITY,
    RESOLVE_APPROVAL_ACTIVITY,
    RUN_CANCELLED_ERROR,
    ApprovalOutcome,
    ApprovalRef,
    RequestApprovalInput,
    ResolveApprovalInput,
)


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
        queued = await approvals.queue_message(db, run, block, approvals.REQUESTED_EVENT)
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
