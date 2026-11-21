"""Migration 4.15 (N.1): the Approval block's columns and the approval endings, as the database
enforces them (ADR-016 §9a, §10)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.models.user import User
from app.routers.pipelines import run_summary
from tests.test_run_endings_schema import _refused, _run


def _approval(run_id, status="waiting", **fields) -> PipelineRunBlock:
    return PipelineRunBlock(
        pipeline_run_id=run_id,
        block_id="approve",
        position=1,
        type="approval",
        status=status,
        **fields,
    )


async def test_a_waiting_approval_records_its_deadline(db_user):
    run_id = await _run(db_user.id, status="running")
    deadline = datetime.now(UTC) + timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        db.add(_approval(run_id, status_reason="awaiting_approval", approval_deadline=deadline))
        await db.commit()
    async with AsyncSessionLocal() as db:
        block = await db.scalar(
            select(PipelineRunBlock).where(PipelineRunBlock.pipeline_run_id == run_id)
        )
    assert (block.status, block.status_reason, block.approval_deadline) == (
        "waiting",
        "awaiting_approval",
        deadline,
    )


@pytest.mark.parametrize("answer", ["approved", "rejected", "expired", "superseded"])
async def test_every_answer_is_admitted(db_user, answer):
    run_id = await _run(db_user.id, status="running")
    async with AsyncSessionLocal() as db:
        db.add(_approval(run_id, status="completed", approval_answer=answer))
        await db.commit()


async def test_an_unknown_answer_is_refused(db_user):
    await _refused(
        "ck_pipeline_run_blocks_approval_answer",
        _approval(await _run(db_user.id, status="running"), approval_answer="maybe"),
    )


async def test_awaiting_approval_sits_only_on_a_waiting_block(db_user):
    await _refused(
        "ck_pipeline_run_blocks_status_reason_status",
        _approval(
            await _run(db_user.id, status="running"),
            status="running",
            status_reason="awaiting_approval",
        ),
    )


@pytest.mark.parametrize("reason", ["approval_rejected", "approval_expired", "approval_superseded"])
async def test_each_approval_ending_is_a_stop_and_reads_back(db_user, reason):
    run_id = await _run(db_user.id, stopped_at_block_id="approve", stop_reason=reason)
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, run_id)
    assert run_summary(run, 1).stop_reason == reason


async def test_an_approval_ending_is_completed_only(db_user):
    with pytest.raises(IntegrityError) as err:
        await _run(
            db_user.id,
            status="failed",
            stopped_at_block_id="approve",
            stop_reason="approval_rejected",
        )
    assert "ck_pipeline_runs_stopped_is_completed" in str(err.value)


async def test_the_answerer_and_the_superseding_run_are_forgotten_when_deleted(db_user, other_user):
    answerer = other_user
    run_id = await _run(db_user.id, status="running")
    later_id = await _run(db_user.id, status="running")
    async with AsyncSessionLocal() as db:
        db.add(
            _approval(
                run_id,
                status="completed",
                approval_answer="superseded",
                answered_at=datetime.now(UTC),
                answered_by=answerer.id,
                superseded_by_run_id=later_id,
            )
        )
        await db.commit()
        await db.execute(delete(User).where(User.id == answerer.id))
        await db.delete(await db.get(PipelineRun, later_id))
        await db.commit()
    async with AsyncSessionLocal() as db:
        block = await db.scalar(
            select(PipelineRunBlock).where(PipelineRunBlock.pipeline_run_id == run_id)
        )
    assert (block.approval_answer, block.answered_by, block.superseded_by_run_id) == (
        "superseded",
        None,
        None,
    )
