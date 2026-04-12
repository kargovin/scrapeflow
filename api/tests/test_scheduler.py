"""Tests for the scheduler background task (Step 20).

Tests call the internal helpers _dispatch_due_jobs and _recover_stale_pending directly,
using real DB sessions and mocked NATS JetStream — the same pattern as test_jobs.py's
result consumer tests.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy import select

from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.scheduler import _dispatch_due_jobs, _recover_stale_pending
from app.models.job import Job
from app.models.job_runs import JobRun

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def due_job(db_user):
    """A scheduled job whose next_run_at is 1 minute in the past."""
    job = Job(
        user_id=db_user.id,
        url="https://example.com",
        schedule_cron="* * * * *",
        schedule_status="active",
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield job
    # Cleanup is handled by db_user cascade


@pytest_asyncio.fixture
async def future_job(db_user):
    """A scheduled job whose next_run_at is 5 minutes in the future (not yet due)."""
    job = Job(
        user_id=db_user.id,
        url="https://example.com",
        schedule_cron="* * * * *",
        schedule_status="active",
        next_run_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield job


@pytest_asyncio.fixture
async def stale_pending_run(db_user):
    """A job + pending JobRun created 15 minutes ago (past the 10-minute stale threshold)."""
    job = Job(user_id=db_user.id, url="https://example.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.flush()
        run = JobRun(
            job_id=job.id,
            status="pending",
            created_at=datetime.now(UTC) - timedelta(minutes=15),
        )
        db.add(run)
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
    yield job, run


@pytest_asyncio.fixture
async def fresh_pending_run(db_user):
    """A pending JobRun created 5 minutes ago (below the 10-minute stale threshold)."""
    job = Job(user_id=db_user.id, url="https://example.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.flush()
        run = JobRun(
            job_id=job.id,
            status="pending",
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(run)
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
    yield job, run


# ---------------------------------------------------------------------------
# Tests: _dispatch_due_jobs
# ---------------------------------------------------------------------------


async def test_scheduler_dispatches_due_job(due_job):
    """A due cron job gets a new pending JobRun and a NATS publish."""
    mock_js = AsyncMock()

    await _dispatch_due_jobs(AsyncSessionLocal, mock_js)

    # One pending JobRun should have been created
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(JobRun).where(JobRun.job_id == due_job.id, JobRun.status == "pending")
        )
        run = result.scalar_one_or_none()

    assert run is not None, "Expected a new pending JobRun"
    assert mock_js.publish.call_count == 1

    # Verify the published subject and payload
    subject, raw = mock_js.publish.call_args.args
    assert subject == NATS_JOBS_RUN_HTTP_SUBJECT
    payload = json.loads(raw.decode())
    assert payload["job_id"] == str(due_job.id)
    assert payload["run_id"] == str(run.id)
    assert payload["url"] == due_job.url

    # next_run_at should have advanced from its original value
    async with AsyncSessionLocal() as db:
        updated = await db.get(Job, due_job.id)
    assert updated.next_run_at is not None
    assert updated.next_run_at > due_job.next_run_at  # advanced, not necessarily > now()
    assert updated.last_run_at is not None


async def test_scheduler_skips_future_job(future_job):
    """A job whose next_run_at is in the future must not be dispatched."""
    mock_js = AsyncMock()

    await _dispatch_due_jobs(AsyncSessionLocal, mock_js)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(JobRun).where(JobRun.job_id == future_job.id))
        run = result.scalar_one_or_none()

    assert run is None
    mock_js.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _recover_stale_pending
# ---------------------------------------------------------------------------


async def test_scheduler_recovers_stale_pending_run(stale_pending_run):
    """A pending run older than 10 minutes is re-published to NATS (no new JobRun)."""
    job, run = stale_pending_run
    mock_js = AsyncMock()

    await _recover_stale_pending(AsyncSessionLocal, mock_js)

    # The fixture's run_id must appear in one of the publish calls.
    # (Other stale runs from unrelated tests may also be published.)
    published_run_ids = [
        json.loads(call.args[1].decode())["run_id"] for call in mock_js.publish.call_args_list
    ]
    assert str(run.id) in published_run_ids

    # Verify the subject used for this job (engine="http" → HTTP subject)
    matching_calls = [
        call
        for call in mock_js.publish.call_args_list
        if json.loads(call.args[1].decode())["run_id"] == str(run.id)
    ]
    assert matching_calls[0].args[0] == NATS_JOBS_RUN_HTTP_SUBJECT

    # No new JobRun should have been inserted for this job
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(JobRun).where(JobRun.job_id == job.id))
        all_runs = result.scalars().all()
    assert len(all_runs) == 1
    assert all_runs[0].id == run.id

    # Status must still be pending — recovery never writes to the DB
    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run.id)
    assert updated_run.status == "pending"


async def test_scheduler_ignores_fresh_pending_run(fresh_pending_run):
    """A pending run younger than 10 minutes must not be re-published."""
    _, run = fresh_pending_run
    mock_js = AsyncMock()

    await _recover_stale_pending(AsyncSessionLocal, mock_js)

    # The fresh run's run_id must NOT appear in any publish call.
    # (Other stale runs from unrelated tests may be published — that is fine.)
    published_run_ids = [
        json.loads(call.args[1].decode())["run_id"] for call in mock_js.publish.call_args_list
    ]
    assert str(run.id) not in published_run_ids
