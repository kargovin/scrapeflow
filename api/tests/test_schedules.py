"""F.1 — Temporal Schedules for v2 job schedules: admission per meter, the API's arming, the cap."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode

from app.core import schedules
from app.core.db import AsyncSessionLocal
from app.core.dispatch import build_scrape_input
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.quota_views import quota_run_units
from app.models.user_quota import UserQuota
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities import schedules as admission
from app.workflows.activities.schedules import STORAGE_FULL, admit_scheduled_run
from app.workflows.job import JobRunInput
from app.workflows.queues import WORKFLOW_QUEUE
from app.workflows.scheduled_job import AdmitInput, ScheduledJobInput, ScheduledJobWorkflow

HELD = 2


async def _job(user_id, **fields) -> Job:
    values = {
        "url": "https://example.com",
        "schedule_cron": "0 * * * *",
        "schedule_status": "active",
        "schedule_lane": "v2",
    } | fields
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, **values)
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


async def _with_run(job: Job) -> Job:
    """PATCH answers with the job's latest run; a job made through the API has one."""
    async with AsyncSessionLocal() as db:
        db.add(JobRun(job_id=job.id, status="completed", lane="v2"))
        await db.commit()
    return job


async def _quota(user_id, **limits):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=user_id, **limits))
        await db.commit()


async def _runs(job_id) -> list[JobRun]:
    async with AsyncSessionLocal() as db:
        return list((await db.scalars(select(JobRun).where(JobRun.job_id == job_id))).all())


async def _get(job_id) -> Job:
    async with AsyncSessionLocal() as db:
        return await db.get(Job, job_id)


async def _units(user_id) -> int:
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(func.count())
            .select_from(quota_run_units)
            .where(quota_run_units.c.user_id == user_id)
        )


def _admit(job, run_id=None, last_chance=False):
    return admit_scheduled_run(
        AdmitInput(job_id=str(job.id), run_id=str(run_id or uuid.uuid4()), last_chance=last_chance)
    )


# ---------------------------------------------------------------------------
# admit_scheduled_run — per meter (ADR-010 §1)
# ---------------------------------------------------------------------------


async def test_an_admitted_tick_inserts_a_pending_v2_run_and_returns_its_input(db_user):
    job = await _job(db_user.id, engine="http", output_format="markdown")
    run_id = uuid.uuid4()

    result = await _admit(job, run_id)

    assert result.verdict == "admitted"
    [run] = await _runs(job.id)
    assert (run.id, run.status, run.lane) == (run_id, "pending", "v2")
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job.id)
        run = await db.get(JobRun, run_id)
        expected = JobRunInput(
            run_id=str(run_id), engine="http", scrape=build_scrape_input(job, run, None)
        )
    assert result.run == expected
    assert job.last_run_at is not None
    # v2's clock is the Schedule: v1's loop selects on next_run_at, so it stays NULL.
    assert job.next_run_at is None


async def test_a_retried_admission_returns_the_run_it_inserted(db_user):
    job = await _job(db_user.id)
    run_id = uuid.uuid4()

    first = await _admit(job, run_id)
    # Paused meanwhile: the row is this tick's run all the same, or it would hold a slot forever.
    async with AsyncSessionLocal() as db:
        (await db.get(Job, job.id)).schedule_status = "paused"
        await db.commit()
    again = await _admit(job, run_id)

    assert again == first
    assert len(await _runs(job.id)) == 1


async def test_a_monthly_breach_skips_the_tick_without_a_counted_row(db_user):
    await _quota(db_user.id, monthly_runs_limit=1)
    filler = await _job(db_user.id, schedule_cron=None, schedule_status=None, schedule_lane=None)
    async with AsyncSessionLocal() as db:
        db.add(JobRun(job_id=filler.id, status="completed", lane="v2"))
        await db.commit()
    job = await _job(db_user.id)
    before = await _units(db_user.id)

    result = await _admit(job)

    assert result.verdict == "skipped"
    assert await _runs(job.id) == []
    assert await _units(db_user.id) == before
    job = await _get(job.id)
    assert job.schedule_skip_reason == "monthly_runs"
    assert job.schedule_skipped_at is not None


async def test_a_storage_breach_fails_the_tick_with_the_remedy(db_user):
    await _quota(db_user.id, storage_bytes_limit=100, storage_bytes_used=100)
    job = await _job(db_user.id, webhook_url="https://hooks.example.com/in")
    run_id = uuid.uuid4()

    result = await _admit(job, run_id)

    assert result.verdict == "failed"
    [run] = await _runs(job.id)
    assert (run.id, run.status, run.error, run.lane) == (run_id, "failed", STORAGE_FULL, "v2")
    assert "delete old results" in run.error
    async with AsyncSessionLocal() as db:
        [delivery] = (
            await db.scalars(select(WebhookDelivery).where(WebhookDelivery.run_id == run_id))
        ).all()
    assert delivery.event == "job.failed"
    assert delivery.payload["error"] == STORAGE_FULL
    # A retry finds it and does not admit.
    assert (await _admit(job, run_id)).verdict == "failed"


async def test_a_full_concurrency_meter_waits_and_marks_the_job_waiting(db_user):
    await _quota(db_user.id, concurrent_jobs_limit=1)
    filler = await _job(db_user.id, schedule_cron=None, schedule_status=None, schedule_lane=None)
    async with AsyncSessionLocal() as db:
        db.add(JobRun(job_id=filler.id, status="running", lane="v2"))
        await db.commit()
    job = await _job(db_user.id)

    assert (await _admit(job)).verdict == "waiting"
    waiting_since = (await _get(job.id)).schedule_waiting_since
    assert waiting_since is not None
    assert (await _admit(job)).verdict == "waiting"
    assert (await _get(job.id)).schedule_waiting_since == waiting_since
    assert await _runs(job.id) == []

    assert (await _admit(job, last_chance=True)).verdict == "skipped"
    job = await _get(job.id)
    assert (job.schedule_skip_reason, job.schedule_waiting_since) == ("concurrent_jobs", None)


async def test_a_waiting_tick_is_admitted_once_the_slot_frees(db_user):
    await _quota(db_user.id, concurrent_jobs_limit=1)
    filler = await _job(db_user.id, schedule_cron=None, schedule_status=None, schedule_lane=None)
    async with AsyncSessionLocal() as db:
        busy = JobRun(job_id=filler.id, status="running", lane="v2")
        db.add(busy)
        await db.commit()
    job = await _job(db_user.id)
    assert (await _admit(job)).verdict == "waiting"

    async with AsyncSessionLocal() as db:
        (await db.get(JobRun, busy.id)).status = "completed"
        await db.commit()

    assert (await _admit(job)).verdict == "admitted"
    assert (await _get(job.id)).schedule_waiting_since is None


@pytest.mark.parametrize(
    "fields",
    [
        {"schedule_status": "paused"},
        {"schedule_lane": "v1"},
        {"schedule_cron": None, "schedule_status": None, "schedule_lane": None},
    ],
)
async def test_a_tick_on_a_job_no_longer_armed_on_v2_is_stopped(db_user, fields):
    job = await _job(db_user.id, **fields)
    assert (await _admit(job)).verdict == "stopped"
    assert await _runs(job.id) == []


async def test_a_tick_on_a_deleted_job_is_stopped():
    result = await admit_scheduled_run(
        AdmitInput(job_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()))
    )
    assert result.verdict == "stopped"


async def test_ticks_waking_together_admit_only_up_to_the_limit(db_user, monkeypatch):
    """N waiting ticks re-ask at once when slots free: the lock lets only the limit through."""
    await _quota(db_user.id, concurrent_jobs_limit=2)
    jobs = [await _job(db_user.id) for _ in range(5)]

    counted = admission.is_quota_exceeded

    async def slow_count(user_id, db, quota_type):
        exceeded = await counted(user_id, db, quota_type)
        if quota_type == "concurrent_jobs":
            # Widen count → insert: unserialised, every tick counts zero and inserts.
            await asyncio.sleep(0.2)
        return exceeded

    monkeypatch.setattr(admission, "is_quota_exceeded", slow_count)
    results = await asyncio.gather(*(_admit(job) for job in jobs))

    assert sorted(r.verdict for r in results) == ["admitted"] * 2 + ["waiting"] * 3


async def test_admission_waits_on_the_users_lock(db_user):
    job = await _job(db_user.id)
    async with AsyncSessionLocal() as holder:
        from app.core.quota import lock_user

        await lock_user(db_user.id, holder)
        task = asyncio.create_task(_admit(job))
        await asyncio.sleep(HELD)
        assert await _runs(job.id) == [], "admitted under a held lock"
        await holder.rollback()
    assert (await asyncio.wait_for(task, timeout=10)).verdict == "admitted"


# ---------------------------------------------------------------------------
# The Schedule itself (ADR-013)
# ---------------------------------------------------------------------------


def test_every_schedule_carries_adr_013s_three_policies():
    job = Job(id=uuid.uuid4(), schedule_cron="*/5 * * * *", schedule_status="active")
    schedule = schedules.build(job)

    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.policy.catchup_window == timedelta(days=365)
    assert schedule.policy.pause_on_failure is False
    assert schedule.spec.cron_expressions == ["*/5 * * * *"]
    assert schedule.state.paused is False
    action = schedule.action
    assert action.workflow == ScheduledJobWorkflow.__temporal_workflow_definition.name
    assert action.args == [ScheduledJobInput(job_id=str(job.id))]
    assert action.id == f"scheduled-job-{job.id}"
    assert action.task_queue == WORKFLOW_QUEUE


def test_a_paused_schedule_is_put_paused():
    job = Job(id=uuid.uuid4(), schedule_cron="0 * * * *", schedule_status="paused")
    assert schedules.build(job).state.paused is True


def test_v2_next_run_is_the_crons_next_tick_and_none_when_paused():
    now = datetime(2026, 10, 27, 10, 7, tzinfo=UTC)
    job = Job(schedule_cron="*/15 * * * *", schedule_status="active", schedule_lane="v2")
    assert schedules.next_run(job, now) == datetime(2026, 10, 27, 10, 15, tzinfo=UTC)
    job.schedule_status = "paused"
    assert schedules.next_run(job, now) is None


# ---------------------------------------------------------------------------
# The API arms, pauses and removes Schedules (F.1) and enforces the cap (ADR-013)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest.fixture
def temporal():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    client.create_schedule = AsyncMock()
    handle = MagicMock()
    handle.update = AsyncMock()
    handle.delete = AsyncMock()
    client.get_schedule_handle = MagicMock(return_value=handle)
    workflow_handle = MagicMock()
    workflow_handle.cancel = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=workflow_handle)
    app.state.temporal = client
    yield client
    app.state.temporal = None


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setattr(settings, "job_lane", "v2")


SCHEDULED = {"url": "https://example.com/s", "schedule_cron": "*/10 * * * *"}


async def _create(client, headers, body=SCHEDULED):
    with _resolves_public():
        return await client.post("/jobs", json=body, headers=headers)


def _resolves_public():
    from unittest.mock import patch

    return patch(
        "socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))]
    )


async def test_a_job_created_with_a_cron_is_armed_on_v2_with_a_schedule(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    resp = await _create(client, headers)
    assert resp.status_code == 201
    job = await _get(uuid.UUID(resp.json()["id"]))

    assert (job.schedule_status, job.schedule_lane, job.next_run_at) == ("active", "v2", None)
    [call] = temporal.create_schedule.call_args_list
    sid, schedule = call.args
    assert sid == f"job-schedule-{job.id}"
    assert schedule.spec.cron_expressions == ["*/10 * * * *"]
    assert schedule.state.paused is False
    # The first run still goes at once, as before.
    assert len(temporal.start_workflow.call_args_list) == 1


async def test_a_schedule_that_cannot_be_armed_pauses_it_and_fails_the_first_run(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    temporal.create_schedule.side_effect = RuntimeError("temporal down")
    resp = await _create(client, headers)

    assert resp.status_code == 503
    job_id = uuid.UUID(resp.json()["detail"]["job_id"])
    assert (await _get(job_id)).schedule_status == "paused"
    [run] = await _runs(job_id)
    assert run.status == "failed"
    temporal.start_workflow.assert_not_called()


async def test_an_existing_schedule_is_replaced_not_duplicated(temporal):
    temporal.create_schedule.side_effect = ScheduleAlreadyRunningError()
    job = Job(id=uuid.uuid4(), schedule_cron="0 * * * *", schedule_status="paused")

    await schedules.put(temporal, job)

    [call] = temporal.get_schedule_handle.return_value.update.call_args_list
    update = call.args[0](MagicMock())
    assert update.schedule.state.paused is True


async def test_pause_resume_and_remove_drive_the_schedule(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    job_id = (await _create(client, headers)).json()["id"]
    temporal.create_schedule.reset_mock()
    temporal.create_schedule.side_effect = ScheduleAlreadyRunningError()
    handle = temporal.get_schedule_handle.return_value

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_status": "paused"}, headers=headers
    )
    assert resp.status_code == 200
    assert handle.update.call_args.args[0](MagicMock()).schedule.state.paused is True

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_status": "active"}, headers=headers
    )
    assert resp.status_code == 200
    assert handle.update.call_args.args[0](MagicMock()).schedule.state.paused is False

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_cron": "0 9 * * *"}, headers=headers
    )
    assert resp.status_code == 200
    assert handle.update.call_args.args[0](MagicMock()).schedule.spec.cron_expressions == [
        "0 9 * * *"
    ]

    resp = await client.patch(f"/jobs/{job_id}", json={"schedule_cron": None}, headers=headers)
    assert resp.status_code == 200
    handle.delete.assert_awaited_once()
    job = await _get(uuid.UUID(job_id))
    assert (job.schedule_cron, job.schedule_status, job.schedule_lane) == (None, None, None)


async def test_a_resume_the_engine_refuses_leaves_the_schedule_paused(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    job_id = (await _create(client, headers)).json()["id"]
    await client.patch(f"/jobs/{job_id}", json={"schedule_status": "paused"}, headers=headers)
    temporal.create_schedule.side_effect = RuntimeError("temporal down")

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_status": "active"}, headers=headers
    )

    assert resp.status_code == 503
    assert (await _get(uuid.UUID(job_id))).schedule_status == "paused"


async def test_a_pause_the_engine_refuses_still_pauses_the_row(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    job_id = (await _create(client, headers)).json()["id"]
    temporal.create_schedule.side_effect = RuntimeError("temporal down")

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_status": "paused"}, headers=headers
    )

    # Admission reads the row, so every tick the Schedule still fires is stopped.
    assert resp.status_code == 200
    assert (await _get(uuid.UUID(job_id))).schedule_status == "paused"


@pytest.mark.parametrize(
    "body",
    [{"schedule_status": "sometimes"}, {"schedule_status": None}],
)
async def test_a_bad_schedule_status_is_422(
    client, headers, db_user, temporal, v2, mock_jetstream, body
):
    job_id = (await _create(client, headers)).json()["id"]
    resp = await client.patch(f"/jobs/{job_id}", json=body, headers=headers)
    assert resp.status_code == 422


async def test_pausing_a_job_without_a_schedule_is_422(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    job_id = (await _create(client, headers, {"url": "https://example.com/once"})).json()["id"]
    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_status": "paused"}, headers=headers
    )
    assert resp.status_code == 422


async def test_a_permanent_delete_removes_the_schedule(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    job_id = (await _create(client, headers)).json()["id"]
    resp = await client.delete(f"/jobs/{job_id}?permanent=true", headers=headers)
    assert resp.status_code == 204
    temporal.get_schedule_handle.assert_called_with(f"job-schedule-{job_id}")
    temporal.get_schedule_handle.return_value.delete.assert_awaited_once()


async def test_deleting_a_schedule_already_gone_is_fine(temporal):
    temporal.get_schedule_handle.return_value.delete.side_effect = RPCError(
        "not found", RPCStatusCode.NOT_FOUND, b""
    )
    await schedules.delete(temporal, uuid.uuid4())


# --- the cap (ADR-013 §2, §5) ------------------------------------------------


async def _at_cap(user_id, limit=1, paused=0):
    await _quota(user_id, scheduled_jobs_limit=limit)
    for _ in range(limit):
        await _job(user_id)
    for _ in range(paused):
        await _job(user_id, schedule_status="paused")
    return user_id


async def test_creating_a_scheduled_job_past_the_cap_is_429(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    await _at_cap(db_user.id, limit=1)

    resp = await _create(client, headers)

    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "scheduled_jobs"
    temporal.create_schedule.assert_not_called()
    # A job with no schedule is not capped.
    assert (await _create(client, headers, {"url": "https://example.com/1"})).status_code == 201


async def test_paused_schedules_do_not_count(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    user_id = await _at_cap(db_user.id, limit=2, paused=3)
    async with AsyncSessionLocal() as db:
        # One of the two active ones paused: room for one.
        job = await db.scalar(
            select(Job).where(Job.user_id == user_id, Job.schedule_status == "active").limit(1)
        )
        job.schedule_status = "paused"
        await db.commit()

    assert (await _create(client, headers)).status_code == 201


async def test_resuming_past_the_cap_is_429(client, headers, db_user, temporal, v2, mock_jetstream):
    user_id = await _at_cap(db_user.id, limit=1)
    paused = await _with_run(await _job(user_id, schedule_status="paused"))

    resp = await client.patch(
        f"/jobs/{paused.id}", json={"schedule_status": "active"}, headers=headers
    )

    assert resp.status_code == 429
    assert (await _get(paused.id)).schedule_status == "paused"


async def test_resuming_one_below_the_cap_is_allowed(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    # The row is already `active` in the session when the count runs (autoflush): without
    # excluding it, a user one below the cap could never resume.
    await _quota(db_user.id, scheduled_jobs_limit=1)
    paused = await _with_run(await _job(db_user.id, schedule_status="paused"))

    resp = await client.patch(
        f"/jobs/{paused.id}", json={"schedule_status": "active"}, headers=headers
    )

    assert resp.status_code == 200
    assert (await _get(paused.id)).schedule_status == "active"


async def test_adding_a_cron_past_the_cap_is_429_but_changing_one_is_not(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    user_id = await _at_cap(db_user.id, limit=1)
    plain = await _with_run(
        await _job(user_id, schedule_cron=None, schedule_status=None, schedule_lane=None)
    )
    async with AsyncSessionLocal() as db:
        armed = await db.scalar(
            select(Job).where(Job.user_id == user_id, Job.schedule_status == "active")
        )
    await _with_run(armed)

    resp = await client.patch(
        f"/jobs/{plain.id}", json={"schedule_cron": "0 * * * *"}, headers=headers
    )
    assert resp.status_code == 429
    assert (await _get(plain.id)).schedule_cron is None

    resp = await client.patch(
        f"/jobs/{armed.id}", json={"schedule_cron": "30 * * * *"}, headers=headers
    )
    assert resp.status_code == 200


async def test_the_cap_check_waits_on_the_users_lock(
    client, headers, db_user, temporal, v2, mock_jetstream
):
    paused = await _with_run(await _job(db_user.id, schedule_status="paused"))

    async with AsyncSessionLocal() as holder:
        from app.core.quota import lock_user

        await lock_user(db_user.id, holder)
        task = asyncio.create_task(
            client.patch(f"/jobs/{paused.id}", json={"schedule_status": "active"}, headers=headers)
        )
        await asyncio.sleep(HELD)
        assert (await _get(paused.id)).schedule_status == "paused", "armed under a held lock"
        await holder.rollback()
    assert (await asyncio.wait_for(task, timeout=10)).status_code == 200


def test_the_default_cap_is_twenty():
    assert settings.default_quota_scheduled_jobs == 20
