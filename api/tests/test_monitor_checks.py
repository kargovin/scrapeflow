"""L.6 — ending a check: the failure counter, self-pause, monitor events, and what the mirrors
write for a check (ADR-016 §4a, §5e, §7, §8)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from sqlalchemy import select

from app.core import monitor_events
from app.core.db import AsyncSessionLocal
from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.core.pipeline_trigger import mark_not_started
from app.core.quota import lock_user
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion
from app.models.webhook_delivery import WebhookDelivery
from app.pipelines.validator import validate
from app.settings import settings
from app.workflows.activities.db import mirror_block_status, mirror_run_status
from app.workflows.pipeline import MirrorBlockInput, MirrorRunInput

DEFINITION = validate(
    {
        "blocks": [
            {"id": "fetch", "type": "scrape", "config": {"url": "https://example.com"}},
            {
                "id": "gate",
                "type": "validate",
                "input": "fetch",
                "config": {"rules": [{"kind": "changed"}], "when_unmet": "stop"},
            },
            {
                "id": "notify",
                "type": "webhook",
                "input": "gate",
                "config": {
                    "url": "https://example.com/hook",
                    "events": ["pipeline.result", "pipeline.failed"],
                },
            },
        ]
    }
).definition


async def _monitor(user_id, state="active", failures=0) -> Monitor:
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(user_id=user_id, name=f"m-{uuid.uuid4().hex[:8]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition=DEFINITION)
        db.add(version)
        await db.flush()
        monitor = Monitor(
            user_id=user_id,
            name=pipeline.name,
            pipeline_id=pipeline.id,
            pipeline_version_id=version.id,
            schedule_cron="*/5 * * * *",
            state=state,
            consecutive_failures=failures,
        )
        db.add(monitor)
        await db.commit()
        await db.refresh(monitor)
        return monitor


async def _check(monitor: Monitor | None, user_id=None, pipeline=None) -> PipelineRun:
    run_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        run = PipelineRun(
            id=run_id,
            pipeline_id=pipeline[0] if pipeline else monitor.pipeline_id,
            pipeline_version_id=pipeline[1] if pipeline else monitor.pipeline_version_id,
            user_id=user_id or monitor.user_id,
            status="running",
            workflow_id=f"pipeline-run-{run_id}",
            monitor_id=monitor.id if monitor else None,
        )
        db.add(run)
        db.add_all(
            PipelineRunBlock(
                pipeline_run_id=run_id, block_id=b, position=i, type=t, status="pending"
            )
            for i, (b, t) in enumerate(
                (("fetch", "scrape"), ("gate", "validate"), ("notify", "webhook"))
            )
        )
        await db.commit()
        return run


async def _end(run: PipelineRun, status: str, **fields) -> str:
    if status == "failed":
        fields.setdefault("error", "fetch: blocked")
        fields.setdefault("failed_block_id", "fetch")
    return await mirror_run_status(MirrorRunInput(run_id=str(run.id), status=status, **fields))


async def _get(model, id_):
    async with AsyncSessionLocal() as db:
        return await db.get(model, id_)


async def _events(monitor_id) -> list[MonitorEvent]:
    async with AsyncSessionLocal() as db:
        return list(
            await db.scalars(
                select(MonitorEvent)
                .where(MonitorEvent.monitor_id == monitor_id)
                .order_by(MonitorEvent.first_at)
            )
        )


@pytest.fixture
def paused_schedules():
    with patch("app.core.monitor_checks.monitor_schedules.pause", new=AsyncMock()) as pause:
        yield pause


# --- the counter -------------------------------------------------------------------------


async def test_a_retried_mirror_moves_the_counter_once(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)

    assert await _end(run, "failed") == "failed"
    assert await _end(run, "failed") == "failed"

    assert (await _get(Monitor, monitor.id)).consecutive_failures == 1


async def test_five_failures_pause_the_monitor_and_record_why(db_user, paused_schedules):
    monitor = await _monitor(db_user.id, failures=3)
    await _end(await _check(monitor), "failed")
    assert (await _get(Monitor, monitor.id)).state == "active"
    paused_schedules.assert_not_awaited()

    fifth = await _check(monitor)
    await _end(fifth, "failed", error="extract: 401 invalid key", failed_block_id="extract")

    row = await _get(Monitor, monitor.id)
    assert (row.state, row.consecutive_failures) == ("paused_itself", 5)
    [event] = await _events(monitor.id)
    assert (event.kind, event.count, event.ended_at) == ("paused_itself", 1, None)
    assert event.detail == {
        "failed_run_id": str(fifth.id),
        "error": "extract: 401 invalid key",
        "failures": 5,
    }
    paused_schedules.assert_awaited_once()
    assert paused_schedules.await_args.args[1].id == monitor.id


async def test_a_success_in_between_resets_the_count(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    for _ in range(4):
        await _end(await _check(monitor), "failed")
    await _end(
        await _check(monitor), "completed", stopped_at_block_id="gate", stop_reason="rule_not_met"
    )
    for _ in range(4):
        await _end(await _check(monitor), "failed")

    row = await _get(Monitor, monitor.id)
    assert (row.state, row.consecutive_failures) == ("active", 4)
    paused_schedules.assert_not_awaited()


async def test_a_cancelled_check_moves_nothing(db_user, paused_schedules):
    monitor = await _monitor(db_user.id, failures=4)

    await _end(await _check(monitor), "cancelled")

    row = await _get(Monitor, monitor.id)
    assert (row.state, row.consecutive_failures) == ("active", 4)


async def test_a_user_paused_monitor_counts_but_keeps_its_pause(db_user, paused_schedules):
    monitor = await _monitor(db_user.id, state="paused", failures=4)

    await _end(await _check(monitor), "failed")

    row = await _get(Monitor, monitor.id)
    assert (row.state, row.consecutive_failures) == ("paused", 5)
    paused_schedules.assert_not_awaited()


async def test_a_hand_runs_failure_moves_nothing(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    hand = await _check(
        None, user_id=db_user.id, pipeline=(monitor.pipeline_id, monitor.pipeline_version_id)
    )

    assert await _end(hand, "failed") == "failed"
    assert (await _get(Monitor, monitor.id)).consecutive_failures == 0


async def test_a_check_that_could_not_start_counts_and_alerts(db_user, paused_schedules):
    monitor = await _monitor(db_user.id, failures=4)
    run = await _check(monitor)
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, run.id)
        blocks = list(
            await db.scalars(
                select(PipelineRunBlock).where(PipelineRunBlock.pipeline_run_id == run.id)
            )
        )

        def no_client():
            raise RuntimeError("engine down")

        await mark_not_started(db, run, blocks, RuntimeError("engine down"), no_client)

    row = await _get(Monitor, monitor.id)
    assert (row.state, row.consecutive_failures) == ("paused_itself", 5)
    async with AsyncSessionLocal() as db:
        [alert] = list(
            await db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.pipeline_run_id == run.id)
            )
        )
    assert alert.event == "pipeline.failed"
    assert (await _get(PipelineRun, run.id)).error == "could not start workflow: engine down"


# --- what the mirrors write ---------------------------------------------------------------


async def test_the_run_mirror_writes_a_checks_columns_with_its_outcome(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)
    summary = {"status": "ok", "kind": "page", "lines": ["-a", "+b"]}

    await _end(
        run,
        "completed",
        baseline_eligible=True,
        change_status="changed",
        change_summary=summary,
        captured_values={"last_price": {"value": 8}},
    )

    row = await _get(PipelineRun, run.id)
    assert row.baseline_eligible is True
    assert (row.change_status, row.change_summary) == ("changed", summary)
    assert row.captured_values == {"last_price": {"value": 8}}


async def test_baseline_eligible_is_never_written_on_a_failed_run(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)

    await _end(run, "failed", baseline_eligible=True, change_status="changed")

    row = await _get(PipelineRun, run.id)
    assert (row.baseline_eligible, row.change_status) == (False, "changed")


async def test_a_retried_run_mirror_does_not_rewrite_the_outcome(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)
    await _end(run, "completed", baseline_eligible=True, change_status="first_check")

    await _end(run, "completed", change_status="changed")

    row = await _get(PipelineRun, run.id)
    assert (row.baseline_eligible, row.change_status) == (True, "first_check")


async def test_the_block_mirror_writes_the_gates_fingerprint(db_user):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)
    await mirror_block_status(
        MirrorBlockInput(run_id=str(run.id), block_id="gate", status="running")
    )

    await mirror_block_status(
        MirrorBlockInput(
            run_id=str(run.id),
            block_id="gate",
            status="completed",
            output_ref="b/k",
            fingerprint="f" * 64,
        )
    )

    async with AsyncSessionLocal() as db:
        fingerprint = await db.scalar(
            select(PipelineRunBlock.fingerprint).where(
                PipelineRunBlock.pipeline_run_id == run.id, PipelineRunBlock.block_id == "gate"
            )
        )
    assert fingerprint == "f" * 64


async def test_the_live_status_carries_the_change_status(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)
    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    received: list[dict] = []
    await conn.add_listener(PIPELINE_STATUS_CHANNEL, lambda *a: received.append(json.loads(a[3])))
    try:
        await _end(
            run,
            "completed",
            change_status="unchanged",
            stopped_at_block_id="gate",
            stop_reason="rule_not_met",
        )
        await asyncio.sleep(0.3)
    finally:
        await conn.close()

    [update] = [m for m in received if m["run_id"] == str(run.id)]
    assert update["change_status"] == "unchanged"


async def test_the_run_mirror_takes_the_users_lock_first_for_a_check(db_user, paused_schedules):
    monitor = await _monitor(db_user.id)
    run = await _check(monitor)
    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(_end(run, "failed"))
        await asyncio.sleep(1)
        assert not task.done(), "the mirror did not wait for the user's lock"
        # ...and it had not locked the run row: a reader with NOWAIT still gets it.
        await holder.execute(
            select(PipelineRun.id).where(PipelineRun.id == run.id).with_for_update(nowait=True)
        )
        await holder.rollback()
    assert await asyncio.wait_for(task, timeout=10) == "failed"


# --- events -------------------------------------------------------------------------------


async def _record(monitor_id, kind, detail=None):
    async with AsyncSessionLocal() as db:
        monitor = await db.get(Monitor, monitor_id)
        await lock_user(monitor.user_id, db)
        inserted = await monitor_events.record(db, monitor, kind, datetime.now(UTC), detail)
        await db.commit()
        return inserted


async def test_identical_firings_are_one_counted_situation(db_user):
    monitor = await _monitor(db_user.id)

    first = await _record(monitor.id, "skipped_monthly_runs", {"resets_at": "2026-12-01"})
    again = [await _record(monitor.id, "skipped_monthly_runs") for _ in range(3)]

    assert first is not None and again == [None, None, None]
    [event] = await _events(monitor.id)
    assert (event.count, event.ended_at, event.detail) == (4, None, {"resets_at": "2026-12-01"})


async def test_a_different_situation_ends_the_open_one(db_user):
    monitor = await _monitor(db_user.id)
    await _record(monitor.id, "skipped_monthly_runs")
    await _record(monitor.id, "skipped_check_in_progress")
    await _record(monitor.id, "skipped_monthly_runs")

    events = await _events(monitor.id)
    assert [(e.kind, e.count, e.ended_at is None) for e in events] == [
        ("skipped_monthly_runs", 1, False),
        ("skipped_check_in_progress", 1, False),
        ("skipped_monthly_runs", 1, True),
    ]
