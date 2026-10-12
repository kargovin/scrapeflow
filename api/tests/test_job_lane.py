"""D.1 + D.4 — the job lane marker (ADR-009 §7 mechanism 4) and the dispatch switch."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio

from app.core.db import AsyncSessionLocal
from app.core.result_consumer import _handle_result
from app.core.scheduler import _recover_stale_pending
from app.models.job import Job
from app.models.job_runs import JobRun

STALE = timedelta(minutes=15)  # past the 10-minute stale_pending_threshold


def _published_run_ids(mock_js: AsyncMock) -> set[str]:
    return {json.loads(c.args[1].decode())["run_id"] for c in mock_js.publish.call_args_list}


@pytest_asyncio.fixture
async def stale_runs_on_both_lanes(db_user):
    """One stale pending run per lane, on the same job."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        created = datetime.now(UTC) - STALE
        v1 = JobRun(job_id=job.id, status="pending", lane="v1", created_at=created)
        v2 = JobRun(job_id=job.id, status="pending", lane="v2", created_at=created)
        db.add_all([v1, v2])
        await db.commit()
    return v1, v2


async def test_a_new_run_is_on_v1_unless_told_otherwise(db_user):
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending")
        db.add(run)
        await db.commit()
        await db.refresh(run)
    assert run.lane == "v1"


async def test_recovery_republishes_v1_runs_only(stale_runs_on_both_lanes):
    v1, v2 = stale_runs_on_both_lanes
    mock_js = AsyncMock()

    await _recover_stale_pending(AsyncSessionLocal, mock_js)

    published = _published_run_ids(mock_js)
    assert str(v1.id) in published
    # Temporal owns the v2 run; a NATS copy would scrape it twice.
    assert str(v2.id) not in published


async def test_the_result_consumer_discards_a_result_for_a_v2_run(db_user):
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running", lane="v2")
        db.add(run)
        await db.commit()
        run_id = run.id

    msg = MagicMock()
    msg.data = json.dumps(
        {
            "run_id": str(run_id),
            "status": "completed",
            "minio_path": f"scrapeflow-results/history/{run_id}/scrape.html",
        }
    ).encode()
    msg.metadata = MagicMock()
    msg.ack = AsyncMock()

    await _handle_result(msg, AsyncMock(), MagicMock())

    msg.ack.assert_called_once()
    async with AsyncSessionLocal() as db:
        unchanged = await db.get(JobRun, run_id)
    assert (unchanged.status, unchanged.result_path) == ("running", None)
