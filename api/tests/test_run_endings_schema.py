"""Migration 4.10 (J.3): the run/block ending columns and the pipeline alert rows, as the
database enforces them (ADR-014 §5a, §8b)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.webhook_delivery import WebhookDelivery

VERDICT = {
    "rule_index": 0,
    "rule": {"kind": "compare", "path": "/price", "op": "lt", "value": 100, "negate": False},
    "found": 120,
    "expected": 100,
}


async def _run(user_id, status="completed", **fields) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(user_id=user_id, name=f"endings-{uuid.uuid4().hex[:8]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition={"blocks": []})
        db.add(version)
        await db.flush()
        run_id = uuid.uuid4()
        db.add(
            PipelineRun(
                id=run_id,
                pipeline_id=pipeline.id,
                pipeline_version_id=version.id,
                user_id=user_id,
                status=status,
                workflow_id=f"pipeline-run-{run_id}",
                **fields,
            )
        )
        await db.commit()
    return run_id


def _block(run_id, block_id="notify", status="skipped", **fields) -> PipelineRunBlock:
    return PipelineRunBlock(
        pipeline_run_id=run_id,
        block_id=block_id,
        position=0,
        type="webhook",
        status=status,
        **fields,
    )


def _alert(pipeline_run_id, block_id="notify", **fields) -> WebhookDelivery:
    return WebhookDelivery(
        pipeline_run_id=pipeline_run_id,
        block_id=block_id,
        event="pipeline.failed",
        webhook_url="https://hooks.example.net/in",
        payload={"event": "pipeline.failed"},
        **fields,
    )


async def _refused(constraint: str, *rows) -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(rows)
        with pytest.raises(IntegrityError) as err:
            await db.commit()
    assert constraint in str(err.value)


async def test_a_stopped_run_records_where_why_and_the_verdict(db_user):
    run_id = await _run(db_user.id, stopped_at_block_id="check", stop_reason="rule_not_met")
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                PipelineRunBlock(
                    pipeline_run_id=run_id,
                    block_id="check",
                    position=0,
                    type="validate",
                    status="completed",
                    verdict=VERDICT,
                ),
                PipelineRunBlock(
                    pipeline_run_id=run_id,
                    block_id="notify",
                    position=1,
                    type="webhook",
                    status="skipped",
                    status_reason="stopped",
                    reason_block_id="check",
                ),
            ]
        )
        await db.commit()
        blocks = {
            b.block_id: b
            for b in await db.scalars(
                select(PipelineRunBlock).where(PipelineRunBlock.pipeline_run_id == run_id)
            )
        }
    assert blocks["check"].verdict == VERDICT
    assert (blocks["notify"].status_reason, blocks["notify"].reason_block_id) == (
        "stopped",
        "check",
    )


@pytest.mark.parametrize(
    "reason", ["stopped", "upstream_failed", "cancelled", "not_started", "alert_only"]
)
async def test_every_skip_reason_is_admitted(db_user, reason):
    run_id = await _run(db_user.id, status="failed")
    async with AsyncSessionLocal() as db:
        db.add(_block(run_id, status_reason=reason))
        await db.commit()


async def test_a_block_with_no_reason_is_still_admitted(db_user):
    # Rows from before 4.10: "reason not recorded".
    run_id = await _run(db_user.id, status="failed")
    async with AsyncSessionLocal() as db:
        db.add(_block(run_id))
        await db.commit()


async def test_an_unknown_skip_reason_is_refused(db_user):
    await _refused(
        "ck_pipeline_run_blocks_status_reason",
        _block(await _run(db_user.id), status_reason="because"),
    )


@pytest.mark.parametrize("status", ["pending", "running", "completed", "failed"])
async def test_a_reason_only_sits_on_a_skipped_or_waiting_block(db_user, status):
    await _refused(
        "ck_pipeline_run_blocks_status_reason_status",
        _block(await _run(db_user.id), status=status, status_reason="stopped"),
    )


@pytest.mark.parametrize("undo", ["undoing", "undone", "undo_failed", "kept", "not_undoable"])
async def test_a_delivered_block_takes_every_undo_status(db_user, undo):
    run_id = await _run(db_user.id, status="failed", failed_block_id="notify")
    async with AsyncSessionLocal() as db:
        db.add(
            _block(
                run_id,
                "store",
                status="completed",
                delivery={"destination_id": str(uuid.uuid4()), "kind": "bucket", "location": "b/k"},
                undo_status=undo,
                undo_error="gave up" if undo == "undo_failed" else None,
            )
        )
        await db.commit()


async def test_only_a_completed_block_can_be_undone(db_user):
    await _refused(
        "ck_pipeline_run_blocks_undo_status_completed",
        _block(await _run(db_user.id), status="failed", undo_status="undone"),
    )


async def test_an_unknown_undo_status_is_refused(db_user):
    await _refused(
        "ck_pipeline_run_blocks_undo_status",
        _block(await _run(db_user.id), status="completed", undo_status="reverted"),
    )


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
async def test_only_a_completed_run_can_be_stopped(db_user, status):
    with pytest.raises(IntegrityError) as err:
        await _run(
            db_user.id, status=status, stopped_at_block_id="check", stop_reason="rule_not_met"
        )
    assert "ck_pipeline_runs_stopped_is_completed" in str(err.value)


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"stopped_at_block_id": "check"}, "ck_pipeline_runs_stop_reason_with_block"),
        ({"stop_reason": "rule_not_met"}, "ck_pipeline_runs_stop_reason_with_block"),
        ({"stopped_at_block_id": "check", "stop_reason": "bored"}, "ck_pipeline_runs_stop_reason"),
    ],
)
async def test_a_stop_names_its_block_and_a_known_reason(db_user, fields, constraint):
    with pytest.raises(IntegrityError) as err:
        await _run(db_user.id, **fields)
    assert constraint in str(err.value)


async def test_a_run_rolling_back_is_running_with_a_failed_block_and_a_deadline(db_user):
    from datetime import UTC, datetime, timedelta

    deadline = datetime.now(UTC) + timedelta(minutes=10)
    run_id = await _run(
        db_user.id, status="running", failed_block_id="notify", rollback_deadline=deadline
    )
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, run_id)
    assert (run.failed_block_id, run.rollback_deadline) == ("notify", deadline)


# ---------------------------------------------------------------------------
# webhook_deliveries: a pipeline run's failure alert
# ---------------------------------------------------------------------------


async def test_a_pipeline_alert_row_is_admitted_and_goes_with_its_run(db_user):
    run_id = await _run(db_user.id, status="failed", failed_block_id="extract")
    async with AsyncSessionLocal() as db:
        db.add(_alert(run_id))
        await db.commit()
        assert await db.scalar(
            select(WebhookDelivery.id).where(WebhookDelivery.pipeline_run_id == run_id)
        )
        await db.delete(await db.get(PipelineRun, run_id))
        await db.commit()
        assert (
            await db.scalar(
                select(WebhookDelivery.id).where(WebhookDelivery.pipeline_run_id == run_id)
            )
            is None
        )


async def test_the_same_alert_is_not_queued_twice(db_user):
    run_id = await _run(db_user.id, status="failed")
    async with AsyncSessionLocal() as db:
        db.add(_alert(run_id))
        await db.commit()
    await _refused("uq_webhook_deliveries_pipeline_run_block_event", _alert(run_id))


async def test_two_blocks_of_one_run_each_queue_their_alert(db_user):
    run_id = await _run(db_user.id, status="failed")
    async with AsyncSessionLocal() as db:
        db.add_all([_alert(run_id, "notify"), _alert(run_id, "pager")])
        await db.commit()


async def test_a_pipeline_alert_names_its_block(db_user):
    await _refused(
        "ck_webhook_deliveries_pipeline_block",
        _alert(await _run(db_user.id, status="failed"), block_id=None),
    )


async def test_a_block_id_needs_a_pipeline_run():
    # CHECKs fire before foreign keys, so a made-up crawl id reaches the CHECK.
    await _refused(
        "ck_webhook_deliveries_pipeline_block",
        WebhookDelivery(
            crawl_id=uuid.uuid4(),
            block_id="notify",
            event="crawl.completed",
            webhook_url="https://hooks.example.net/in",
            payload={},
        ),
    )


@pytest.mark.parametrize(
    ("other_owner", "constraint"),
    [
        ("run_id", "ck_webhook_deliveries_run_or_crawl"),
        ("job_id", "ck_webhook_deliveries_job_or_batch_or_crawls"),
        ("crawl_id", "ck_webhook_deliveries_job_or_batch_or_crawls"),
    ],
)
async def test_a_pipeline_alert_has_no_second_owner(db_user, other_owner, constraint):
    run_id = await _run(db_user.id, status="failed")
    await _refused(constraint, _alert(run_id, **{other_owner: uuid.uuid4()}))
