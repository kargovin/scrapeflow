"""F.7 — a recurring job reads as one: schedule fields on job reads, the filter, the cap usage."""

from datetime import UTC, datetime

import pytest_asyncio
from croniter import croniter

from app.core.db import AsyncSessionLocal
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user_quota import UserQuota
from tests.test_admin import admin_api_key, admin_headers, admin_user  # noqa: F401

SKIPPED_AT = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


async def _job(user_id, **fields) -> Job:
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, url="https://example.com", **fields)
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="completed", lane="v2"))
        await db.commit()
        await db.refresh(job)
    return job


def _scheduled(**fields):
    return {
        "schedule_cron": "0 * * * *",
        "schedule_status": "active",
        "schedule_lane": "v2",
    } | fields


async def test_a_job_read_carries_its_schedule(client, headers, db_user):
    job = await _job(
        db_user.id,
        **_scheduled(schedule_skipped_at=SKIPPED_AT, schedule_skip_reason="monthly_runs"),
    )

    body = (await client.get(f"/jobs/{job.id}", headers=headers)).json()

    assert body["schedule_cron"] == "0 * * * *"
    assert body["schedule_status"] == "active"
    expected = croniter("0 * * * *", datetime.now(UTC)).get_next(datetime)
    assert datetime.fromisoformat(body["next_run_at"]) == expected
    assert datetime.fromisoformat(body["schedule_skipped_at"]) == SKIPPED_AT
    assert body["schedule_skip_reason"] == "monthly_runs"
    assert body["schedule_waiting_since"] is None


async def test_a_paused_schedule_has_no_next_run(client, headers, db_user):
    job = await _job(db_user.id, **_scheduled(schedule_status="paused"))
    body = (await client.get(f"/jobs/{job.id}", headers=headers)).json()
    assert (body["schedule_status"], body["next_run_at"]) == ("paused", None)


async def test_a_one_off_job_reads_with_no_schedule(client, headers, db_user):
    job = await _job(db_user.id)
    body = (await client.get(f"/jobs/{job.id}", headers=headers)).json()
    assert (body["schedule_cron"], body["schedule_status"], body["next_run_at"]) == (
        None,
        None,
        None,
    )


async def test_the_scheduled_filter_on_the_job_list(client, headers, db_user):
    active = await _job(db_user.id, **_scheduled())
    paused = await _job(db_user.id, **_scheduled(schedule_status="paused"))
    once = await _job(db_user.id)

    scheduled = (await client.get("/jobs?scheduled=true", headers=headers)).json()
    one_off = (await client.get("/jobs?scheduled=false", headers=headers)).json()

    assert {j["id"] for j in scheduled} == {str(active.id), str(paused.id)}
    assert {j["id"] for j in one_off} == {str(once.id)}


async def test_another_users_scheduled_job_is_404(client, headers, other_user):
    job = await _job(other_user.id, **_scheduled())
    assert (await client.get(f"/jobs/{job.id}", headers=headers)).status_code == 404
    listed = (await client.get("/jobs?scheduled=true", headers=headers)).json()
    assert str(job.id) not in {j["id"] for j in listed}


async def test_the_admin_job_list_filters_and_carries_the_schedule(
    client,
    admin_headers,  # noqa: F811
    db_user,
):
    job = await _job(db_user.id, **_scheduled())
    await _job(db_user.id)

    resp = await client.get(
        f"/admin/jobs?scheduled=true&user_id={db_user.id}", headers=admin_headers
    )

    [row] = resp.json()
    assert (row["id"], row["schedule_cron"], row["schedule_status"]) == (
        str(job.id),
        "0 * * * *",
        "active",
    )
    detail = (await client.get(f"/admin/jobs/{job.id}", headers=admin_headers)).json()
    assert detail["next_run_at"] is not None


async def test_the_owner_sees_active_schedules_against_the_cap(client, headers, db_user):
    await _job(db_user.id, **_scheduled())
    await _job(db_user.id, **_scheduled())
    await _job(db_user.id, **_scheduled(schedule_status="paused"))
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=db_user.id, scheduled_jobs_limit=7))
        await db.commit()

    body = (await client.get("/users/me/scheduled-jobs", headers=headers)).json()

    assert body == {"active": 2, "limit": 7}


async def test_the_default_cap_applies_without_an_override(client, headers, db_user):
    body = (await client.get("/users/me/scheduled-jobs", headers=headers)).json()
    assert body == {"active": 0, "limit": 20}


async def test_an_admin_reads_and_sets_the_scheduled_jobs_override(
    client,
    admin_headers,  # noqa: F811
    db_user,
):
    before = await client.get(f"/admin/users/{db_user.id}/quota", headers=admin_headers)
    assert before.status_code == 200
    assert before.json()["scheduled_jobs_limit"] is None

    resp = await client.patch(
        f"/admin/users/{db_user.id}/quota",
        json={"scheduled_jobs_limit": 3},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    after = await client.get(f"/admin/users/{db_user.id}/quota", headers=admin_headers)
    assert after.json()["scheduled_jobs_limit"] == 3
