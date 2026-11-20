"""M.8 — rollback (ADR-014 §7): the rollback mirrors, the alert's rollback lists."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from sqlalchemy import func, select, update
from temporalio.exceptions import ApplicationError

from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.models.quota_views import quota_active_submissions
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities.db import (
    mirror_rollback_started,
    mirror_run_status,
    mirror_undo_status,
)
from app.workflows.pipeline import MirrorRunInput, MirrorUndoInput, RollbackStartedInput
from tests.test_pipeline_alerts import ALERT_URL, _definition, _webhook
from tests.test_pipeline_alerts import _run as _alerting_run
from tests.test_workflows_pipeline import _block, _run_rows

LOCATION = {
    "endpoint": "https://s3.example.com/",
    "bucket": "exports",
    "region": None,
    "key": "out/p1/r1/store.json",
}
RECORD = {"destination_id": str(uuid.uuid4()), "kind": "bucket", "location": LOCATION}
DEADLINE = datetime(2026, 11, 21, 14, 20, tzinfo=UTC)
ROLLING_BACK = (
    ("fetch", "scrape", "completed"),
    ("store", "bucket", "completed"),
    ("notify", "webhook", "failed"),
    ("after", "webhook", "pending"),
)


async def _delivered(run_id, *block_ids):
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == run_id,
                PipelineRunBlock.block_id.in_(block_ids),
            )
            .values(delivery=RECORD)
        )
        await db.commit()


def _started(run_id) -> RollbackStartedInput:
    return RollbackStartedInput(
        run_id=str(run_id),
        failed_block_id="notify",
        error="notify: HTTP 503 (gave up after 5 attempts)",
        rollback_deadline=DEADLINE,
    )


async def _listen():
    conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    received: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.put_nowait(a[3]))
    return conn, received


# --- mirror_rollback_started ----------------------------------------------------------------------


async def test_rollback_started_keeps_the_run_running_and_records_what_broke(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    conn, received = await _listen()
    try:
        assert await mirror_rollback_started(_started(run.id)) == "running"
        payload = json.loads(await asyncio.wait_for(received.get(), 5))
    finally:
        await conn.close()

    async with AsyncSessionLocal() as db:
        row = await db.get(PipelineRun, run.id)
    assert (row.status, row.failed_block_id, row.rollback_deadline, row.error) == (
        "running",
        "notify",
        DEADLINE,
        "notify: HTTP 503 (gave up after 5 attempts)",
    )
    assert (payload["status"], payload["rolling_back"]) == ("running", True)


async def test_rollback_started_is_not_refused_for_a_cancel_requested(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK, cancel_requested_at=datetime.now(UTC))
    assert await mirror_rollback_started(_started(run.id)) == "running"
    async with AsyncSessionLocal() as db:
        assert (await db.get(PipelineRun, run.id)).rollback_deadline == DEADLINE


async def test_rollback_started_on_a_deleted_run_reads_as_cancelled(db_user):
    assert await mirror_rollback_started(_started(uuid.uuid4())) == "cancelled"


async def test_a_run_undoing_holds_no_concurrency_slot(db_user):
    # E2: the failed block is failed, the delivered one completed; the undo lives in undo_status.
    run = await _run_rows(db_user.id, ROLLING_BACK)
    await _delivered(run.id, "store")
    await mirror_rollback_started(_started(run.id))
    await mirror_undo_status(MirrorUndoInput(run_id=str(run.id), undo={"store": "undoing"}))
    async with AsyncSessionLocal() as db:
        held = await db.scalar(
            select(func.count())
            .select_from(quota_active_submissions)
            .where(quota_active_submissions.c.submission_id == run.id)
        )
    assert held == 0


# --- mirror_undo_status ----------------------------------------------------------------------------


async def test_an_undo_moves_undoing_then_undone_and_notifies_each(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    await _delivered(run.id, "store")
    rid = str(run.id)
    conn, received = await _listen()
    try:
        assert await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store": "undoing"})) == (
            "running"
        )
        assert (await _block(run.id, "store")).undo_status == "undoing"
        await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store": "undone"}))
        payloads = [json.loads(await asyncio.wait_for(received.get(), 5)) for _ in range(2)]
    finally:
        await conn.close()

    block = await _block(run.id, "store")
    assert (block.status, block.undo_status, block.undo_error, block.delivery) == (
        "completed",
        "undone",
        None,
        RECORD,
    )
    assert [(p["block_id"], p["block_status"], p["block_undo_status"]) for p in payloads] == [
        ("store", "completed", "undoing"),
        ("store", "completed", "undone"),
    ]


async def test_an_undo_that_failed_records_why(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    await _delivered(run.id, "store")
    await mirror_undo_status(
        MirrorUndoInput(
            run_id=str(run.id),
            undo={"store": "undo_failed"},
            error="DestinationRefused: the bucket refused the delete (AccessDenied)",
        )
    )
    block = await _block(run.id, "store")
    assert (block.undo_status, block.undo_error) == (
        "undo_failed",
        "DestinationRefused: the bucket refused the delete (AccessDenied)",
    )


async def test_kept_and_not_undoable_are_one_call(db_user):
    blocks = (
        ("store", "bucket", "completed"),
        ("notify", "webhook", "completed"),
        ("check", "validate", "failed"),
    )
    run = await _run_rows(db_user.id, blocks)
    await _delivered(run.id, "store")
    await mirror_undo_status(
        MirrorUndoInput(run_id=str(run.id), undo={"store": "kept", "notify": "not_undoable"})
    )
    assert [(b, (await _block(run.id, b)).undo_status) for b in ("store", "notify", "check")] == [
        ("store", "kept"),
        ("notify", "not_undoable"),
        ("check", None),
    ]


async def test_an_undo_is_written_on_a_run_whose_cancel_was_requested(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK, cancel_requested_at=datetime.now(UTC))
    await _delivered(run.id, "store")
    await mirror_undo_status(MirrorUndoInput(run_id=str(run.id), undo={"store": "undone"}))
    assert (await _block(run.id, "store")).undo_status == "undone"


async def test_only_a_completed_block_takes_an_undo_status(db_user):
    run = await _run_rows(db_user.id, ROLLING_BACK)
    with pytest.raises(ApplicationError) as exc:
        await mirror_undo_status(MirrorUndoInput(run_id=str(run.id), undo={"notify": "undone"}))
    assert exc.value.non_retryable
    assert (await _block(run.id, "notify")).undo_status is None


async def test_an_undo_on_a_deleted_run_reads_as_cancelled(db_user):
    assert (
        await mirror_undo_status(MirrorUndoInput(run_id=str(uuid.uuid4()), undo={"s": "undone"}))
        == "cancelled"
    )


# --- the failure alert carries the rollback lists (ADR-014 §8c) ---------------------------------

BUCKET_BLOCK = {
    "type": "bucket",
    "input": "fetch",
    "config_schema_version": 1,
    "config": {"destination_id": RECORD["destination_id"]},
    "secrets": {},
}
ROLLED_BACK = _definition(
    {**BUCKET_BLOCK, "id": "store"},
    {**BUCKET_BLOCK, "id": "store_2", "input": "store"},
    {**BUCKET_BLOCK, "id": "store_3", "input": "store_2"},
    _webhook("alert", ALERT_URL, ["pipeline.failed"]),
)


def _entry(block_id):
    return {"block_id": block_id, "kind": "bucket", "location": LOCATION}


async def test_the_alert_is_queued_after_rollback_and_lists_how_each_delivery_ended(db_user):
    run = await _alerting_run(db_user.id, ROLLED_BACK)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(
                PipelineRunBlock.pipeline_run_id == run.id,
                PipelineRunBlock.block_id.in_(("fetch", "store", "store_2", "store_3")),
            )
            .values(status="completed")
        )
        await db.commit()
    await _delivered(run.id, "store", "store_2", "store_3")
    rid = str(run.id)
    await mirror_rollback_started(_started(run.id))
    await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store_3": "undone"}))
    await mirror_undo_status(
        MirrorUndoInput(run_id=rid, undo={"store_2": "undo_failed"}, error="refused")
    )
    await mirror_undo_status(MirrorUndoInput(run_id=rid, undo={"store": "undone"}))
    await mirror_run_status(
        MirrorRunInput(run_id=rid, status="failed", error="store_3: x", failed_block_id="store_3")
    )

    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.payload["rollback"] == {
        "undone": [_entry("store"), _entry("store_3")],
        "left_in_place": [_entry("store_2")],
        "kept": [],
    }


async def test_deliveries_kept_after_an_announcement_are_listed_as_kept(db_user):
    run = await _alerting_run(db_user.id, ROLLED_BACK)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.pipeline_run_id == run.id, PipelineRunBlock.block_id == "store")
            .values(status="completed", delivery=RECORD, undo_status="kept")
        )
        await db.commit()
    await mirror_run_status(
        MirrorRunInput(run_id=str(run.id), status="failed", error="x: y", failed_block_id="x")
    )
    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.payload["rollback"] == {
        "undone": [],
        "left_in_place": [],
        "kept": [_entry("store")],
    }


async def test_a_failure_with_nothing_delivered_lists_nothing(db_user):
    run = await _alerting_run(db_user.id, ROLLED_BACK)
    await mirror_run_status(
        MirrorRunInput(
            run_id=str(run.id), status="failed", error="fetch: x", failed_block_id="fetch"
        )
    )
    async with AsyncSessionLocal() as db:
        [alert] = (
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        ).all()
    assert alert.payload["rollback"] == {"undone": [], "left_in_place": [], "kept": []}
