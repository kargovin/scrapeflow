"""F.3 — scripts/migrate_schedule.py: the per-job move between engines, in the safe order."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import ScheduleAlreadyRunningError

from app.core.db import AsyncSessionLocal
from app.models.job import Job
from app.models.job_runs import JobRun
from scripts import migrate_schedule


def _client(running_actions=0, listed=()):
    client = MagicMock()
    client.create_schedule = AsyncMock(side_effect=ScheduleAlreadyRunningError())
    handle = MagicMock()
    handle.update = AsyncMock()
    handle.delete = AsyncMock()
    handle.describe = AsyncMock(
        return_value=SimpleNamespace(
            info=SimpleNamespace(running_actions=[object()] * running_actions)
        )
    )
    client.get_schedule_handle = MagicMock(return_value=handle)

    async def _list():
        for sid in listed:
            yield SimpleNamespace(id=sid)

    client.list_schedules = AsyncMock(side_effect=lambda *a, **k: _list())
    return client


async def _job(user_id, **fields) -> Job:
    values = {"schedule_cron": "0 * * * *", "schedule_status": "active", "schedule_lane": "v1"}
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, url="https://example.com", **(values | fields))
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


async def _get(job_id) -> Job:
    async with AsyncSessionLocal() as db:
        return await db.get(Job, job_id)


async def _run(job, lane, status="running"):
    async with AsyncSessionLocal() as db:
        db.add(JobRun(job_id=job.id, status=status, lane=lane))
        await db.commit()


async def test_forward_pauses_v1_before_arming_the_schedule(db_user):
    job = await _job(db_user.id)
    client = _client()
    seen = []

    async def put_reads_the_row(*_):
        # Whatever Temporal is told, v1 must already be unable to fire the job.
        seen.append((await _get(job.id)).schedule_status)
        raise ScheduleAlreadyRunningError()

    client.create_schedule.side_effect = put_reads_the_row

    report = await migrate_schedule.forward(AsyncSessionLocal, client, [job.id], apply=True)

    assert report.moved and not report.waiting
    assert seen == ["paused"]
    job = await _get(job.id)
    assert (job.schedule_lane, job.schedule_status, job.next_run_at) == ("v2", "active", None)
    update = client.get_schedule_handle.return_value.update.call_args.args[0](MagicMock())
    assert update.schedule.state.paused is False


async def test_forward_waits_for_a_v1_run_in_flight_and_leaves_the_job_paused(db_user):
    job = await _job(db_user.id)
    await _run(job, "v1")
    client = _client()

    report = await migrate_schedule.forward(AsyncSessionLocal, client, [job.id], apply=True)

    assert report.waiting and not report.moved
    job = await _get(job.id)
    assert (job.schedule_lane, job.schedule_status) == ("v1", "paused")
    client.create_schedule.assert_not_called()


async def test_forward_keeps_a_paused_schedule_paused(db_user):
    job = await _job(db_user.id, schedule_status="paused")
    client = _client()

    await migrate_schedule.forward(AsyncSessionLocal, client, [job.id], apply=True)

    job = await _get(job.id)
    assert (job.schedule_lane, job.schedule_status) == ("v2", "paused")
    update = client.get_schedule_handle.return_value.update.call_args.args[0](MagicMock())
    assert update.schedule.state.paused is True


async def test_a_dry_run_changes_nothing(db_user):
    job = await _job(db_user.id)
    client = _client()

    report = await migrate_schedule.forward(AsyncSessionLocal, client, [job.id], apply=False)

    assert report.moved
    assert (await _get(job.id)).schedule_lane == "v1"
    client.create_schedule.assert_not_called()


async def test_rollback_pauses_the_schedule_then_hands_back_with_a_fresh_clock(db_user):
    job = await _job(db_user.id, schedule_lane="v2")
    client = _client()

    report = await migrate_schedule.rollback(AsyncSessionLocal, client, [job.id], apply=True)

    assert report.moved
    handle = client.get_schedule_handle.return_value
    assert handle.update.call_args.args[0](MagicMock()).schedule.state.paused is True
    handle.delete.assert_awaited_once()
    job = await _get(job.id)
    assert (job.schedule_lane, job.schedule_status) == ("v1", "active")
    assert job.next_run_at > datetime.now(UTC)


@pytest.mark.parametrize("v2_run,open_ticks", [(True, 0), (False, 1)])
async def test_rollback_waits_for_a_v2_run_or_an_open_tick(db_user, v2_run, open_ticks):
    job = await _job(db_user.id, schedule_lane="v2")
    if v2_run:
        await _run(job, "v2", "pending")
    client = _client(running_actions=open_ticks)

    report = await migrate_schedule.rollback(AsyncSessionLocal, client, [job.id], apply=True)

    assert report.waiting and not report.moved
    client.get_schedule_handle.return_value.delete.assert_not_called()
    job = await _get(job.id)
    assert (job.schedule_lane, job.schedule_status) == ("v2", "paused")


async def test_reconcile_puts_v2_schedules_and_deletes_orphans(db_user):
    job = await _job(db_user.id, schedule_lane="v2")
    orphan = f"job-schedule-{uuid.uuid4()}"
    client = _client(listed=[f"job-schedule-{job.id}", orphan, "webhook-sweep"])

    put, deleted = await migrate_schedule.reconcile(AsyncSessionLocal, client, apply=True)

    assert str(job.id) in put
    assert deleted == [orphan]
    client.get_schedule_handle.assert_any_call(orphan)
    client.get_schedule_handle.return_value.delete.assert_awaited_once()
