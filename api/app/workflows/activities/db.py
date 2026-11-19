"""App-DB activities — registered on the workflow-worker queue only; scraper pods have no DB.

Two writers own pipeline_runs.status (ADR-009 §11a): the API's cancel and these mirrors. A
mirror never moves a run out of a terminal state it did not set, and never starts a block on a
run whose cancel was requested. Every write notifies ``pipeline_status`` in its own transaction.
A run deleted while its workflow is still going (ADR-016 §11) reads as ``cancelled``.
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
from app.core.monitor_checks import CheckEnded, after_commit, monitor_check_ended
from app.core.pipeline_alerts import queue_failure_alerts
from app.core.pipeline_status import notify_run
from app.core.quota import lock_user, storage_usage
from app.core.storage import delete_minio_object
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
    RUN_CANCELLED_ERROR,
    MirrorBlockInput,
    MirrorRunInput,
    RecordStorageInput,
    RunRef,
    SkipReason,
    StorageUsage,
)


async def _lock_run(db: AsyncSession, run_id: str) -> PipelineRun | None:
    """None: the run was deleted."""
    return await db.scalar(
        select(PipelineRun).where(PipelineRun.id == uuid.UUID(run_id)).with_for_update()
    )


def _cancel_requested(run: PipelineRun) -> bool:
    return run.status == "cancelled" or run.cancel_requested_at is not None


@activity.defn(name=MIRROR_BLOCK_ACTIVITY)
async def mirror_block_status(input: MirrorBlockInput) -> str:
    """Writes the block's status; returns the run's. ``cancelled`` = the workflow must stop."""
    async with AsyncSessionLocal() as db:
        run = await _lock_run(db, input.run_id)
        if run is None:
            return "cancelled"
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
                values["fingerprint"] = input.fingerprint
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
        # A monitor check may write a monitor event: the user's lock before the run row (§8).
        owner = (
            await db.execute(
                select(PipelineRun.user_id, PipelineRun.monitor_id).where(
                    PipelineRun.id == uuid.UUID(input.run_id)
                )
            )
        ).one_or_none()
        if owner is not None and owner.monitor_id is not None:
            await lock_user(owner.user_id, db)
        run = await _lock_run(db, input.run_id)
        if run is None:
            return "cancelled"
        ended = CheckEnded()
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
            # Only ever on a completed run (§4a).
            run.baseline_eligible = input.baseline_eligible and input.status == "completed"
            if input.change_status is not None:
                run.change_status = input.change_status
            if input.change_summary is not None:
                run.change_summary = input.change_summary
            if input.captured_values is not None:
                run.captured_values = input.captured_values
            # Here and only here: a retried call finds the run terminal and moves nothing.
            ended = await monitor_check_ended(db, run)
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
        await after_commit(_client, ended)
        return run.status


async def _client():
    return activity.client()


@activity.defn(name=RECORD_STORAGE_ACTIVITY)
async def record_storage(input: RecordStorageInput) -> StorageUsage:
    """Charges the block's objects to the run's owner; idempotent on object key (a retry is a
    no-op). Returns the owner's usage after, so the workflow can see the wall (§8d).

    A deleted run: its objects are removed, not charged, and the block ends as cancelled.
    """
    async with AsyncSessionLocal() as db:
        # ⚠️ A delete enumerating the run's ledger rows waits for this, and this for it: without
        # it a row inserted after the enumeration cascades away with its object still on disk.
        user_id = await db.scalar(
            select(PipelineRun.user_id)
            .where(PipelineRun.id == uuid.UUID(input.run_id))
            .with_for_update(read=True, key_share=True)
        )
        if user_id is None:
            await db.rollback()
            await _drop_unrecorded(input)
            raise ApplicationError(
                "the run was deleted", type=RUN_CANCELLED_ERROR, non_retryable=True
            )
        block_row_id = await db.scalar(
            select(PipelineRunBlock.id).where(
                PipelineRunBlock.pipeline_run_id == uuid.UUID(input.run_id),
                PipelineRunBlock.block_id == input.block_id,
            )
        )
        if block_row_id is None:
            raise ApplicationError(
                f"no block {input.block_id!r} in run {input.run_id}", non_retryable=True
            )
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


async def _drop_unrecorded(input: RecordStorageInput) -> None:
    minio = await get_minio()
    left = [
        obj.path
        for obj in input.objects
        if not await delete_minio_object(minio, obj.path, "deleted pipeline run")
    ]
    if left:
        raise ApplicationError(f"{len(left)} objects of a deleted run could not be removed")


@activity.defn(name=CHECK_CANCELLED_ACTIVITY)
async def check_cancelled(input: RunRef) -> bool:
    """15d's fallback for a wait the workflow cannot otherwise interrupt (C.9's backoff)."""
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, uuid.UUID(input.run_id))
        return run is None or _cancel_requested(run)


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
