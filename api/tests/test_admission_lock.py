"""BUG-023 — the quota check and the run insert are serialised per user.

The race tests widen the gap between count and commit, so without the lock every
admission counts the same number and all of them pass.
The wiring tests hold the user's lock from outside and show each admission site waits on it.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.core.quota import admit, lock_user
from app.core.scheduler import _dispatch_due_jobs
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user_quota import UserQuota

PARALLEL = 5
HELD = 2  # well past an unblocked admission, DNS lookups and a cold scheduler pass included


@pytest_asyncio.fixture
async def job(db_user):
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


async def _set_quota(user_id, **limits):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=user_id, **limits))
        await db.commit()


async def _add_runs(job_id, n, status="pending"):
    async with AsyncSessionLocal() as db:
        db.add_all(JobRun(job_id=job_id, status=status, lane="v1") for _ in range(n))
        await db.commit()


async def _run_count(job_id):
    async with AsyncSessionLocal() as db:
        return await db.scalar(select(func.count()).where(JobRun.job_id == job_id))


async def _admit_and_insert(user_id, job_id) -> bool:
    async with AsyncSessionLocal() as db:
        try:
            await admit(user_id, db, storage=False)
        except HTTPException as exc:
            assert exc.status_code == 429
            return False
        db.add(JobRun(job_id=job_id, status="pending", lane="v1"))
        await asyncio.sleep(0.2)
        await db.commit()
        return True


async def test_parallel_admissions_at_the_concurrency_limit_admit_exactly_one(db_user, job):
    await _set_quota(db_user.id, concurrent_jobs_limit=3)
    await _add_runs(job.id, 2)

    admitted = await asyncio.gather(
        *(_admit_and_insert(db_user.id, job.id) for _ in range(PARALLEL))
    )

    assert admitted.count(True) == 1
    assert await _run_count(job.id) == 3


async def test_parallel_admissions_at_the_monthly_limit_admit_exactly_one(db_user, job):
    await _set_quota(db_user.id, monthly_runs_limit=3)
    await _add_runs(job.id, 2, status="completed")

    admitted = await asyncio.gather(
        *(_admit_and_insert(db_user.id, job.id) for _ in range(PARALLEL))
    )

    assert admitted.count(True) == 1
    assert await _run_count(job.id) == 3


async def test_the_lock_does_not_block_rows_that_reference_the_user(db_user):
    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)

        async def insert_job():
            async with AsyncSessionLocal() as db:
                db.add(Job(user_id=db_user.id, url="https://example.com"))
                await db.commit()

        await asyncio.wait_for(insert_job(), timeout=2)
        await holder.rollback()


async def _blocks_while_held(user_id, make_call):
    """Start make_call() under a held lock; it must not finish until the lock is released."""
    async with AsyncSessionLocal() as holder:
        await lock_user(user_id, holder)
        task = asyncio.create_task(make_call())
        await asyncio.sleep(HELD)
        assert not task.done(), "the admission did not wait for the user's lock"
        await holder.rollback()
    return await asyncio.wait_for(task, timeout=10)


async def test_create_job_admits_under_the_lock(quota_client, db_user, headers, job):
    await _set_quota(db_user.id, concurrent_jobs_limit=1)
    await _add_runs(job.id, 1)

    resp = await _blocks_while_held(
        db_user.id,
        lambda: quota_client.post("/jobs", json={"url": "https://example.com"}, headers=headers),
    )

    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "concurrent_jobs"


async def test_create_batch_admits_under_the_lock(quota_client, db_user, headers, job):
    await _set_quota(db_user.id, concurrent_jobs_limit=1)
    await _add_runs(job.id, 1)

    resp = await _blocks_while_held(
        db_user.id,
        lambda: quota_client.post(
            "/batch", json={"urls": ["https://example.com/a"]}, headers=headers
        ),
    )

    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "concurrent_jobs"


async def test_create_crawl_admits_under_the_lock(quota_client, db_user, headers, job):
    await _set_quota(db_user.id, concurrent_jobs_limit=1)
    await _add_runs(job.id, 1)

    resp = await _blocks_while_held(
        db_user.id,
        lambda: quota_client.post(
            "/crawls", json={"seed_url": "https://example.com"}, headers=headers
        ),
    )

    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "concurrent_jobs"


async def test_scheduler_admits_under_the_lock(db_user, mock_jetstream):
    async with AsyncSessionLocal() as db:
        due = Job(
            user_id=db_user.id,
            url="https://example.com",
            schedule_cron="* * * * *",
            schedule_status="active",
            schedule_lane="v1",
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.add(due)
        await db.commit()
        await db.refresh(due)

    async with AsyncSessionLocal() as holder:
        await lock_user(db_user.id, holder)
        task = asyncio.create_task(_dispatch_due_jobs(AsyncSessionLocal, mock_jetstream))
        await asyncio.sleep(HELD)
        assert await _run_count(due.id) == 0, "the scheduler inserted a run under a held lock"
        await holder.rollback()
    await asyncio.wait_for(task, timeout=10)
    assert await _run_count(due.id) == 1
