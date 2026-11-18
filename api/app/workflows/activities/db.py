"""App-DB activities — registered on the workflow-worker queue only; scraper pods have no DB.

Two writers own pipeline_runs.status (ADR-009 §11a): the API's cancel and these mirrors. A
mirror never moves a run out of a terminal state it did not set, and never starts a block on a
run whose cancel was requested. Every write notifies ``pipeline_status`` in its own transaction.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core import webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.ledger import objects_for_pipeline_run, record_object, release_objects
from app.core.pipeline_alerts import queue_failure_alerts
from app.core.pipeline_status import notify_run
from app.core.quota import storage_usage
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.models.storage_object import StorageObject
from app.workflows.activities.job_runs import get_minio
from app.workflows.pipeline import (
    CHECK_CANCELLED_ACTIVITY,
    MIRROR_BLOCK_ACTIVITY,
    MIRROR_RUN_ACTIVITY,
    OUTPUTS_LEFT_ERROR,
    RECORD_STORAGE_ACTIVITY,
    RELEASE_RUN_OUTPUTS_ACTIVITY,
    MirrorBlockInput,
    MirrorRunInput,
    RecordStorageInput,
    RunRef,
    SkipReason,
    StorageUsage,
)


async def _lock_run(db: AsyncSession, run_id: str) -> PipelineRun:
    run = await db.scalar(
        select(PipelineRun).where(PipelineRun.id == uuid.UUID(run_id)).with_for_update()
    )
    if run is None:
        raise ApplicationError(f"no run {run_id}", non_retryable=True)
    return run


def _cancel_requested(run: PipelineRun) -> bool:
    return run.status == "cancelled" or run.cancel_requested_at is not None


@activity.defn(name=MIRROR_BLOCK_ACTIVITY)
async def mirror_block_status(input: MirrorBlockInput) -> str:
    """Writes the block's status; returns the run's. ``cancelled`` = the workflow must stop."""
    async with AsyncSessionLocal() as db:
        run = await _lock_run(db, input.run_id)
        if input.status == "running" and (run.status != "running" or _cancel_requested(run)):
            await notify_run(db, run)
            await db.commit()
            return "cancelled" if _cancel_requested(run) else run.status

        now = datetime.now(UTC)
        if input.status == "running":
            # A retry after `waiting` keeps the first start; the last attempt's error clears.
            values = {
                "status": input.status,
                "input_ref": input.input_ref,
                "started_at": func.coalesce(PipelineRunBlock.started_at, now),
                "error": None,
            }
        elif input.status == "waiting":
            # Between attempts: holds no slot (ADR-009 15a); error is the attempt that failed.
            values = {"status": input.status, "error": input.error}
        elif input.status == "skipped":
            values = {
                "status": input.status,
                "status_reason": input.status_reason,
                "reason_block_id": input.reason_block_id,
            }
        else:
            # A block's outcome is a fact even on a cancelled run: its object exists and is charged.
            values = {
                "status": input.status,
                "output_ref": input.output_ref,
                "error": input.error,
                "finished_at": now,
            }
            if input.status == "completed":
                values["verdict"] = input.verdict
        result = await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == run.id,
                PipelineRunBlock.block_id == input.block_id,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise ApplicationError(
                f"no block {input.block_id!r} in run {input.run_id}", non_retryable=True
            )
        await notify_run(db, run, input.block_id, input.status, input.status_reason)
        await db.commit()
        return run.status


def _skip_reason(run: PipelineRun) -> tuple[SkipReason, str | None] | None:
    """Why the run's pending blocks never run, read from what the run row holds."""
    if run.status == "cancelled":
        return "cancelled", None
    if run.status == "failed":
        return "upstream_failed", run.failed_block_id
    if run.status == "completed" and run.stopped_at_block_id is not None:
        return "stopped", run.stopped_at_block_id
    return None


@activity.defn(name=MIRROR_RUN_ACTIVITY)
async def mirror_run_status(input: MirrorRunInput) -> str:
    """Moves a running run to a terminal status; returns the status the run now holds."""
    async with AsyncSessionLocal() as db:
        run = await _lock_run(db, input.run_id)
        if run.status == "running":
            run.status = input.status
            run.result_path = input.result_path if input.status == "completed" else None
            # C.1: error is always set on failed, NULL on cancelled; a completed run may carry
            # the storage-wall note.
            run.error = None if input.status == "cancelled" else input.error
            run.finished_at = datetime.now(UTC)
            if input.status == "completed":
                run.stopped_at_block_id = input.stopped_at_block_id
                run.stop_reason = input.stop_reason
            elif input.status == "failed":
                run.failed_block_id = input.failed_block_id
        if (skip := _skip_reason(run)) is not None:
            reason, reason_block_id = skip
            await db.execute(
                update(PipelineRunBlock)
                .where(
                    PipelineRunBlock.pipeline_run_id == run.id,
                    PipelineRunBlock.status == "pending",
                )
                .values(status="skipped", status_reason=reason, reason_block_id=reason_block_id)
            )
        # In the transaction that sets failed (ADR-014 §8a); never on a stop or a cancel.
        alerts = input.status == "failed" and run.status == "failed"
        if alerts:
            alerts = await queue_failure_alerts(db, run) > 0
        await notify_run(db, run)
        await db.commit()
        if alerts:
            await webhook_delivery.start_pending(activity.client, db, pipeline_run_id=run.id)
        return run.status


@activity.defn(name=RECORD_STORAGE_ACTIVITY)
async def record_storage(input: RecordStorageInput) -> StorageUsage:
    """Charges the block's objects to the run's owner; idempotent on object key (a retry is a
    no-op). Returns the owner's usage after, so the workflow can see the wall (§8d)."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(PipelineRun.user_id, PipelineRunBlock.id)
                .join(PipelineRunBlock, PipelineRunBlock.pipeline_run_id == PipelineRun.id)
                .where(
                    PipelineRun.id == uuid.UUID(input.run_id),
                    PipelineRunBlock.block_id == input.block_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ApplicationError(
                f"no block {input.block_id!r} in run {input.run_id}", non_retryable=True
            )
        user_id, block_row_id = row
        for obj in input.objects:
            await record_object(
                db,
                user_id=user_id,
                object_key=obj.path,
                size=obj.size,
                pipeline_run_block_id=block_row_id,
            )
        await db.commit()
        used, limit = await storage_usage(user_id, db)
        return StorageUsage(used=used, limit=limit)


@activity.defn(name=CHECK_CANCELLED_ACTIVITY)
async def check_cancelled(input: RunRef) -> bool:
    """15d's fallback for a wait the workflow cannot otherwise interrupt (C.9's backoff)."""
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, uuid.UUID(input.run_id))
        if run is None:
            raise ApplicationError(f"no run {input.run_id}", non_retryable=True)
        return _cancel_requested(run)


@activity.defn(name=RELEASE_RUN_OUTPUTS_ACTIVITY)
async def release_run_outputs(input: RunRef) -> None:
    """A stop's *delete* (ADR-014 §6): the ledger releases every object the run holds.

    Commits what it released, then raises retryable naming what is left — ``release_objects``
    never raises on a failed delete. Each attempt re-reads the rows.
    """
    run_id = uuid.UUID(input.run_id)
    async with AsyncSessionLocal() as db:
        rows = await objects_for_pipeline_run(db, run_id)
        outcome = await release_objects(db, await get_minio(), rows, "pipeline_stop")
        held = (
            select(StorageObject.pipeline_run_block_id)
            .join(PipelineRunBlock, PipelineRunBlock.id == StorageObject.pipeline_run_block_id)
            .where(PipelineRunBlock.pipeline_run_id == run_id)
        )
        await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == run_id,
                PipelineRunBlock.output_ref.is_not(None),
                PipelineRunBlock.collected_at.is_(None),
                PipelineRunBlock.id.not_in(held),
            )
            .values(collected_at=datetime.now(UTC))
        )
        await db.commit()
        if outcome.failed:
            left = sorted(r.object_key for r in await objects_for_pipeline_run(db, run_id))
            raise ApplicationError(
                f"{len(left)} stored objects could not be deleted",
                left,
                type=OUTPUTS_LEFT_ERROR,
            )
