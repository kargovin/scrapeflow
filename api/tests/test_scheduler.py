"""Tests for the scheduler background task (Step 20).

Tests call the internal helpers _dispatch_due_jobs and _recover_stale_pending directly,
using real DB sessions and mocked NATS JetStream — the same pattern as test_jobs.py's
result consumer tests.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.scheduler import _dispatch_due_jobs, _recover_stale_pending
from app.models.batch import Batch, BatchItem
from app.models.job import Job
from app.models.job_runs import JobRun

STALE = timedelta(minutes=15)  # past the 10-minute stale_pending_threshold


def _published_by_run_id(mock_js: AsyncMock) -> dict[str, tuple[str, bytes]]:
    """Map run_id -> (subject, raw bytes) for every publish on the mock.

    Recovery sees every stale run in the shared test DB, not just this test's, so
    tests index by run_id rather than asserting on call_count.
    """
    out = {}
    for call in mock_js.publish.call_args_list:
        subject, raw = call.args
        out[json.loads(raw.decode())["run_id"]] = (subject, raw)
    return out


async def _age_runs(run_ids) -> None:
    """Backdate created_at so the runs fall past the stale threshold."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(JobRun)
            .where(JobRun.id.in_(list(run_ids)))
            .values(created_at=datetime.now(UTC) - STALE)
        )
        await db.commit()


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


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


@pytest_asyncio.fixture
async def stale_pending_batch_run(db_user):
    """A batch + item + pending JobRun on the batch lane, created 15 minutes ago.

    job_id is NULL and batch_item_id is set (ADR-006). Non-default values on the batch
    so the assertions can tell the message came from the batch row, not from defaults.
    """
    async with AsyncSessionLocal() as db:
        batch = Batch(
            user_id=db_user.id,
            status="running",
            output_format="markdown",
            engine="playwright",
            respect_robots=True,
            total=1,
        )
        db.add(batch)
        await db.flush()
        item = BatchItem(batch_id=batch.id, url="https://example.com/batch-item")
        db.add(item)
        await db.flush()
        run = JobRun(
            batch_item_id=item.id,
            status="pending",
            created_at=datetime.now(UTC) - STALE,
        )
        db.add(run)
        await db.commit()
        await db.refresh(batch)
        await db.refresh(item)
        await db.refresh(run)
    yield batch, item, run
    # batches.user_id does not cascade from users; delete the batch (which cascades
    # to items and runs) before db_user tears down.
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Batch).where(Batch.id == batch.id))
        await db.commit()


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
    assert payload["artifact_id"] == str(run.id)
    assert "job_id" not in payload
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


async def test_scheduler_recovers_stale_pending_batch_run(stale_pending_batch_run):
    """A stale batch-lane run is re-published from batch_items + batches (BUG-011).

    Before P9 the recovery SELECT had no lane filter, so batch runs were selected and
    then dropped at db.get(Job, None) — silently, every tick.
    """
    _, item, run = stale_pending_batch_run
    mock_js = AsyncMock()

    await _recover_stale_pending(AsyncSessionLocal, mock_js)

    published = _published_by_run_id(mock_js)
    assert str(run.id) in published, "batch run was not re-published"
    subject, raw = published[str(run.id)]
    payload = json.loads(raw.decode())

    # Routed by the batch's engine, not a job's.
    assert subject == NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
    # Same shape create_batch publishes: item URL, batch settings, no job id, and
    # none of the job-lane extras — the batch lane never sends them.
    assert payload["artifact_id"] == str(run.id)
    assert payload["run_id"] == str(run.id)
    assert payload["url"] == item.url
    assert payload["output_format"] == "markdown"
    assert payload["engine"] == "playwright"
    assert payload["options"]["respect_robots"] is True
    # exclude_none: absent on the wire, not null — same as job_id.
    assert "job_id" not in payload
    assert "credentials" not in payload
    assert "playwright_options" not in payload

    # Recovery never writes: no new run, status still pending.
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(JobRun).where(JobRun.batch_item_id == item.id))
        runs = result.scalars().all()
        assert [r.id for r in runs] == [run.id]
        assert runs[0].status == "pending"


async def test_recovery_republishes_exactly_what_create_batch_sent(
    client, auth_headers, mock_jetstream
):
    """The batch lane's dispatch and recovery build the identical message.

    Both go through core.dispatch.build_batch_scrape_message; this pins that a
    recovered run is byte-for-byte the run that was lost.
    """
    response = await client.post(
        "/batch",
        json={
            "urls": ["https://example.com/x", "https://example.com/y"],
            "output_format": "json",
            "engine": "playwright",
            "respect_robots": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    dispatched = _published_by_run_id(mock_jetstream)
    assert len(dispatched) == 2

    await _age_runs(dispatched.keys())
    recovery_js = AsyncMock()
    await _recover_stale_pending(AsyncSessionLocal, recovery_js)
    recovered = _published_by_run_id(recovery_js)

    for run_id, (subject, raw) in dispatched.items():
        assert run_id in recovered
        assert recovered[run_id] == (subject, raw)


async def test_recovery_republishes_exactly_what_create_job_sent(
    client, auth_headers, mock_jetstream
):
    """The job lane's manual dispatch and recovery build the identical message.

    create_job used to build its message inline from the request body while recovery
    built from the job row; both now go through core.dispatch.build_scrape_message.
    A playwright job with options and actions so every optional field is exercised.
    """
    response = await client.post(
        "/jobs",
        json={
            "url": "https://example.com/manual",
            "engine": "playwright",
            "output_format": "html",
            "respect_robots": True,
            "playwright_options": {"wait_strategy": "load"},
            "actions": [{"type": "wait", "milliseconds": 500}],
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    dispatched = _published_by_run_id(mock_jetstream)
    assert len(dispatched) == 1
    ((run_id, (subject, raw)),) = dispatched.items()
    assert subject == NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
    # Stored via model_dump(), so schema defaults ride along with what was sent.
    assert json.loads(raw.decode())["playwright_options"]["wait_strategy"] == "load"

    await _age_runs([run_id])
    recovery_js = AsyncMock()
    await _recover_stale_pending(AsyncSessionLocal, recovery_js)
    recovered = _published_by_run_id(recovery_js)

    assert recovered[run_id] == (subject, raw)


async def test_scheduler_recovery_logs_when_it_skips(stale_pending_run, monkeypatch):
    """A run the recovery path cannot rebuild is skipped with a warning, never silently.

    The orphan case is unreachable through the schema (CASCADE), so it is forced by
    making the session return no parent. This pins the posture BUG-011 was about: a
    silent `continue` in the only recovery path is how batch runs went unrecovered.
    """
    _, run = stale_pending_run
    monkeypatch.setattr(AsyncSession, "get", AsyncMock(return_value=None))
    mock_js = AsyncMock()

    with capture_logs() as logs:
        await _recover_stale_pending(AsyncSessionLocal, mock_js)

    assert str(run.id) not in _published_by_run_id(mock_js)
    skipped = [
        entry
        for entry in logs
        if entry["log_level"] == "warning" and entry.get("run_id") == str(run.id)
    ]
    assert len(skipped) == 1, logs
    assert "skipping" in skipped[0]["event"]
