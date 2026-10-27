"""F.2 + F.6 — a schedule belongs to one engine (ADR-009 16c), and the recurring-jobs meters see both."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from croniter import croniter

from app.core.db import AsyncSessionLocal
from app.core.minio import get_minio
from app.core.redis import get_redis
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from tests.test_admin import (  # noqa: F401
    _make_mock_minio,
    _make_mock_redis,
    admin_api_key,
    admin_headers,
    admin_user,
)


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest.fixture
def temporal():
    client = MagicMock()
    client.create_schedule = AsyncMock()
    handle = MagicMock()
    handle.update = AsyncMock()
    handle.delete = AsyncMock()
    client.get_schedule_handle = MagicMock(return_value=handle)
    app.state.temporal = client
    yield client
    app.state.temporal = None


async def _job(user_id, **fields) -> Job:
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, url="https://example.com", schedule_cron="0 * * * *", **fields)
        db.add(job)
        await db.flush()
        # PATCH answers with the latest run.
        db.add(JobRun(job_id=job.id, status="completed", lane="v2"))
        await db.commit()
        await db.refresh(job)
    return job


async def _get(job_id) -> Job:
    async with AsyncSessionLocal() as db:
        return await db.get(Job, job_id)


async def test_resuming_a_v2_schedule_keeps_it_on_v2(client, headers, db_user, temporal):
    job = await _job(
        db_user.id,
        schedule_status="paused",
        schedule_lane="v2",
        next_run_at=datetime.now(UTC) - timedelta(hours=1),
    )

    resp = await client.patch(
        f"/jobs/{job.id}", json={"schedule_status": "active"}, headers=headers
    )
    assert resp.status_code == 200

    job = await _get(job.id)
    assert (job.schedule_status, job.schedule_lane, job.next_run_at) == ("active", "v2", None)
    temporal.create_schedule.assert_awaited_once()


async def test_resuming_a_v1_schedule_left_from_before_moves_it_to_v2(
    client, headers, db_user, temporal
):
    # v1's loop is gone (F.6): re-arming on v1 would arm nothing.
    job = await _job(
        db_user.id,
        schedule_status="paused",
        schedule_lane="v1",
        next_run_at=datetime.now(UTC) - timedelta(days=2),
    )

    resp = await client.patch(
        f"/jobs/{job.id}", json={"schedule_status": "active"}, headers=headers
    )
    assert resp.status_code == 200

    job = await _get(job.id)
    assert (job.schedule_status, job.schedule_lane, job.next_run_at) == ("active", "v2", None)
    [call] = temporal.create_schedule.call_args_list
    assert call.args[0] == f"job-schedule-{job.id}"


async def test_pausing_a_v1_schedule_needs_no_engine(client, headers, db_user, temporal):
    job = await _job(db_user.id, schedule_status="active", schedule_lane="v1")

    resp = await client.patch(
        f"/jobs/{job.id}", json={"schedule_status": "paused"}, headers=headers
    )

    assert resp.status_code == 200
    assert (await _get(job.id)).schedule_status == "paused"
    temporal.create_schedule.assert_not_called()


async def test_a_job_gaining_a_schedule_is_armed_on_v2(client, headers, db_user, temporal):
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="completed", lane="v2"))
        await db.commit()

    resp = await client.patch(
        f"/jobs/{job.id}", json={"schedule_cron": "*/30 * * * *"}, headers=headers
    )
    assert resp.status_code == 200
    job = await _get(job.id)
    assert (job.schedule_status, job.schedule_lane) == ("active", "v2")


# --- the meters ----------------------------------------------------------------------------------


async def _stats(client, admin_headers, user_id):  # noqa: F811
    app.dependency_overrides[get_redis] = lambda: _make_mock_redis()
    app.dependency_overrides[get_minio] = lambda: _make_mock_minio()
    try:
        resp = await client.get(f"/admin/stats/users/{user_id}", headers=admin_headers)
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_minio, None)
    assert resp.status_code == 200
    return resp.json()["operational"]


async def test_recurring_jobs_count_both_lanes(client, admin_headers, db_user):  # noqa: F811
    await _job(
        db_user.id,
        schedule_status="active",
        schedule_lane="v1",
        next_run_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    await _job(db_user.id, schedule_status="active", schedule_lane="v2")
    await _job(db_user.id, schedule_status="active", schedule_lane="v2")
    await _job(db_user.id, schedule_status="paused", schedule_lane="v2")

    op = await _stats(client, admin_headers, db_user.id)

    # A schedule moved to Temporal is still a recurring job (16c's first consequence); one left
    # on v1 is counted apart, as nothing fires it.
    assert op["active_recurring_jobs"] == 3
    assert op["active_recurring_jobs_by_lane"] == {"v1": 1, "v2": 2}
    # The next run is v2's cron; the v1 row's old clock fires nothing.
    expected = croniter("0 * * * *", datetime.now(UTC)).get_next(datetime)
    assert datetime.fromisoformat(op["next_scheduled_run_at"]) == expected


async def test_next_run_comes_from_the_cron_when_only_v2_schedules_exist(
    client,
    admin_headers,  # noqa: F811
    db_user,
):
    await _job(db_user.id, schedule_status="active", schedule_lane="v2")

    op = await _stats(client, admin_headers, db_user.id)

    assert op["next_scheduled_run_at"] is not None
    assert op["active_recurring_jobs_by_lane"] == {"v2": 1}
