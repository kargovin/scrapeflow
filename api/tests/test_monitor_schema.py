"""Migration 4.12 (L.2): monitors, monitor events and a check's columns, as the database enforces
them (ADR-016 §2a, §2b, §7b)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.monitor import Monitor, MonitorEvent
from app.models.pipeline import Pipeline, PipelineRun, PipelineRunBlock, PipelineVersion


async def _pipeline(user_id) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as db:
        pipeline = Pipeline(user_id=user_id, name=f"m-{uuid.uuid4().hex[:8]}")
        db.add(pipeline)
        await db.flush()
        version = PipelineVersion(pipeline_id=pipeline.id, version=1, definition={"blocks": []})
        db.add(version)
        await db.commit()
        return pipeline.id, version.id


async def _monitor(user_id, name="m", **fields) -> Monitor:
    pipeline_id, version_id = await _pipeline(user_id)
    async with AsyncSessionLocal() as db:
        monitor = Monitor(
            user_id=user_id,
            name=name,
            pipeline_id=pipeline_id,
            pipeline_version_id=version_id,
            schedule_cron="*/5 * * * *",
            **fields,
        )
        db.add(monitor)
        await db.commit()
        await db.refresh(monitor)
        return monitor


async def _check(monitor: Monitor, **fields) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(
            PipelineRun(
                id=run_id,
                pipeline_id=monitor.pipeline_id,
                pipeline_version_id=monitor.pipeline_version_id,
                user_id=monitor.user_id,
                status="completed",
                workflow_id=f"pipeline-run-{run_id}",
                monitor_id=monitor.id,
                **fields,
            )
        )
        await db.commit()
    return run_id


async def _refused(constraint: str, *rows) -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(rows)
        with pytest.raises(IntegrityError) as err:
            await db.commit()
    assert constraint in str(err.value)


def _event(monitor_id, kind="skipped_monthly_runs", **fields) -> MonitorEvent:
    now = datetime.now(UTC)
    return MonitorEvent(monitor_id=monitor_id, kind=kind, first_at=now, last_at=now, **fields)


async def test_a_monitor_starts_active_with_no_failures_and_its_name_is_unique(db_user):
    monitor = await _monitor(db_user.id)
    assert (monitor.state, monitor.consecutive_failures, monitor.inputs, monitor.carried) == (
        "active",
        0,
        {},
        [],
    )
    pipeline_id, version_id = await _pipeline(db_user.id)
    await _refused(
        "uq_monitors_user_id_name",
        Monitor(
            user_id=db_user.id,
            name="m",
            pipeline_id=pipeline_id,
            pipeline_version_id=version_id,
            schedule_cron="0 * * * *",
        ),
    )


async def test_state_and_kind_are_closed_lists(db_user):
    pipeline_id, version_id = await _pipeline(db_user.id)
    await _refused(
        "ck_monitors_state",
        Monitor(
            user_id=db_user.id,
            name="bad",
            pipeline_id=pipeline_id,
            pipeline_version_id=version_id,
            schedule_cron="0 * * * *",
            state="stopped",
        ),
    )
    monitor = await _monitor(db_user.id, name="ok")
    await _refused("ck_monitor_events_kind", _event(monitor.id, kind="failed"))


async def test_one_open_situation_per_kind(db_user):
    monitor = await _monitor(db_user.id)
    async with AsyncSessionLocal() as db:
        db.add(_event(monitor.id, ended_at=datetime.now(UTC)))
        db.add(_event(monitor.id))
        db.add(_event(monitor.id, kind="waiting_for_slot"))
        await db.commit()
    await _refused("uq_monitor_events_open_kind", _event(monitor.id))


async def test_change_status_is_a_closed_list(db_user):
    monitor = await _monitor(db_user.id)
    for status in ("first_check", "no_baseline", "changed", "unchanged"):
        await _check(monitor, change_status=status)
    with pytest.raises(IntegrityError, match="ck_pipeline_runs_change_status"):
        await _check(monitor, change_status="same")


async def test_deleting_a_monitor_leaves_its_checks_as_ordinary_runs(db_user):
    monitor = await _monitor(db_user.id)
    run_id = await _check(monitor, baseline_eligible=True)
    async with AsyncSessionLocal() as db:
        db.add(_event(monitor.id))
        await db.commit()
        await db.execute(delete(Monitor).where(Monitor.id == monitor.id))
        await db.commit()
        run = await db.get(PipelineRun, run_id)
        assert run.monitor_id is None
        events = await db.scalars(select(MonitorEvent).where(MonitorEvent.monitor_id == monitor.id))
        assert events.all() == []


async def test_deleting_a_baseline_clears_the_link_from_later_checks(db_user):
    monitor = await _monitor(db_user.id)
    baseline = await _check(monitor, baseline_eligible=True, change_status="first_check")
    later = await _check(monitor, baseline_run_id=baseline, change_status="unchanged")
    async with AsyncSessionLocal() as db:
        await db.execute(delete(PipelineRun).where(PipelineRun.id == baseline))
        await db.commit()
        run = await db.get(PipelineRun, later)
    # "Compared with a check since deleted": change_status set, not first_check, no baseline.
    assert (run.change_status, run.baseline_run_id) == ("unchanged", None)


async def test_a_pipeline_a_monitor_uses_cannot_go_by_its_foreign_key(db_user):
    monitor = await _monitor(db_user.id)
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="fk_monitors_pipeline_id"):
            await db.execute(delete(Pipeline).where(Pipeline.id == monitor.pipeline_id))
            await db.commit()


async def test_a_gate_block_records_its_fingerprint(db_user):
    monitor = await _monitor(db_user.id)
    run_id = await _check(monitor)
    async with AsyncSessionLocal() as db:
        db.add(
            PipelineRunBlock(
                pipeline_run_id=run_id,
                block_id="gate",
                position=0,
                type="validate",
                status="completed",
                fingerprint="a" * 64,
            )
        )
        await db.commit()
        fingerprint = await db.scalar(
            select(PipelineRunBlock.fingerprint).where(PipelineRunBlock.pipeline_run_id == run_id)
        )
    assert fingerprint == "a" * 64
