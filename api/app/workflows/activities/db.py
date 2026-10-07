"""App-DB activities — registered on the workflow-worker queue only; scraper pods have no DB."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.db import AsyncSessionLocal
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.workflows.pipeline import (
    MIRROR_BLOCK_ACTIVITY,
    MIRROR_RUN_ACTIVITY,
    MirrorBlockInput,
    MirrorRunInput,
)


@activity.defn(name=MIRROR_BLOCK_ACTIVITY)
async def mirror_block_status(input: MirrorBlockInput) -> None:
    now = datetime.now(UTC)
    if input.status == "running":
        values = {"status": input.status, "input_ref": input.input_ref, "started_at": now}
    else:
        values = {
            "status": input.status,
            "output_ref": input.output_ref,
            "error": input.error,
            "finished_at": now,
        }
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == uuid.UUID(input.run_id),
                PipelineRunBlock.block_id == input.block_id,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise ApplicationError(
                f"no block {input.block_id!r} in run {input.run_id}", non_retryable=True
            )
        await db.commit()


@activity.defn(name=MIRROR_RUN_ACTIVITY)
async def mirror_run_status(input: MirrorRunInput) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == uuid.UUID(input.run_id))
            .values(
                status=input.status,
                result_path=input.result_path,
                error=input.error,
                finished_at=datetime.now(UTC),
            )
        )
        if result.rowcount != 1:
            raise ApplicationError(f"no run {input.run_id}", non_retryable=True)
        if input.status == "failed":
            await db.execute(
                update(PipelineRunBlock)
                .where(
                    PipelineRunBlock.pipeline_run_id == uuid.UUID(input.run_id),
                    PipelineRunBlock.status == "pending",
                )
                .values(status="skipped")
            )
        await db.commit()
