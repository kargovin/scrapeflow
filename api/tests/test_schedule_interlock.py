"""F.2 — a schedule belongs to one engine (ADR-009 16c), and the recurring-jobs meters see both."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from croniter import croniter
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.minio import get_minio
from app.core.redis import get_redis
from app.core.scheduler import _dispatch_due_jobs
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from app.settings import settings
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


async def _pending_runs(job_id) -> int:
    async with AsyncSessionLocal() as db:
        return len(
            (
                await db.scalars(
                    select(JobRun).where(JobRun.job_id == job_id, JobRun.status == "pending")
                )
            ).all()
        )


async def test_resuming_a_v2_schedule_never_re_arms_it_on_v1(
    client, headers, db_user, temporal, monkeypatch
):
    # Even with the switch back on v1, and a stale v1 clock on the row.
    monkeypatch.setattr(settings, "schedule_lane", "v1")
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
    await _dispatch_due_jobs(AsyncSessionLocal, AsyncMock())
    assert await _pending_runs(job.id) == 0


async def test_resuming_a_v1_schedule_keeps_it_on_v1(
    client, headers, db_user, temporal, monkeypatch
):
    monkeypatch.setattr(settings, "schedule_lane", "v2")
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
    assert (job.schedule_status, job.schedule_lane) == ("active", "v1")
    # Its clock restarts from now: a resume replays no missed ticks.
    assert job.next_run_at > datetime.now(UTC)
    temporal.create_schedule.assert_not_called()


async def test_a_job_gaining_a_schedule_takes_the_switchs_lane(
    client, headers, db_user, temporal, monkeypatch
):
    monkeypatch.setattr(settings, "schedule_lane", "v2")
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


async def test_the_v1_loop_fires_v1_schedules_only(db_user, mock_jetstream):
    due = datetime.now(UTC) - timedelta(minutes=1)
    v1 = await _job(db_user.id, schedule_status="active", schedule_lane="v1", next_run_at=due)
    v2 = await _job(db_user.id, schedule_status="active", schedule_lane="v2", next_run_at=due)

    await _dispatch_due_jobs(AsyncSessionLocal, mock_jetstream)

    assert await _pending_runs(v1.id) == 1
    assert await _pending_runs(v2.id) == 0


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
        next_run_at=datetime.now(UTC) + timedelta(days=3),
    )
    await _job(db_user.id, schedule_status="active", schedule_lane="v2")
    await _job(db_user.id, schedule_status="active", schedule_lane="v2")
    await _job(db_user.id, schedule_status="paused", schedule_lane="v2")

    op = await _stats(client, admin_headers, db_user.id)

    # A schedule moved to Temporal is still a recurring job (16c's first consequence).
    assert op["active_recurring_jobs"] == 3
    assert op["active_recurring_jobs_by_lane"] == {"v1": 1, "v2": 2}
    # v2's hourly cron fires before v1's clock, three days out.
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
