"""D.1 + D.4 — the job lane marker (ADR-009 §7 mechanism 4) and dispatch; v2 only since G.1."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from temporalio.common import WorkflowIDReusePolicy

from app.core.db import AsyncSessionLocal
from app.core.dispatch import build_scrape_input, build_scrape_message
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.webhook_delivery import WebhookDelivery
from app.workflows.job import JobRunInput, JobWorkflow
from app.workflows.queues import WORKFLOW_QUEUE


async def test_a_new_run_is_on_v2_unless_told_otherwise(db_user):
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending")
        db.add(run)
        await db.commit()
        await db.refresh(run)
    assert run.lane == "v2"


# ---------------------------------------------------------------------------
# D.4 — the dispatch switch
# ---------------------------------------------------------------------------

PLAYWRIGHT_JOB = {
    "url": "https://example.com/manual",
    "engine": "playwright",
    "output_format": "html",
    "respect_robots": True,
    "playwright_options": {"wait_strategy": "load"},
    "actions": [{"type": "wait", "milliseconds": 500}],
    # Resolved by the SSRF check at create time.
    "webhook_url": "https://example.com/hook",
}


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


@pytest.fixture
def temporal():
    """A fake Temporal client where lazy_client finds it."""
    client = MagicMock()
    client.start_workflow = AsyncMock()
    handle = MagicMock()
    handle.cancel = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    app.state.temporal = client
    yield client
    app.state.temporal = None


@pytest_asyncio.fixture
async def created_jobs():
    ids: list[str] = []
    yield ids
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Job).where(Job.id.in_([uuid.UUID(i) for i in ids])))
        await db.commit()


async def _runs(job_id) -> list[JobRun]:
    async with AsyncSessionLocal() as db:
        return list(
            (await db.scalars(select(JobRun).where(JobRun.job_id == uuid.UUID(job_id)))).all()
        )


async def test_a_new_job_starts_its_workflow(client, auth_headers, temporal, created_jobs):
    response = await client.post("/jobs", json=PLAYWRIGHT_JOB, headers=auth_headers)
    assert response.status_code == 201
    job_id = response.json()["id"]
    created_jobs.append(job_id)

    [run] = await _runs(job_id)
    assert (run.lane, run.status) == ("v2", "pending")
    [call] = temporal.start_workflow.call_args_list
    workflow, run_input = call.args
    assert workflow == JobWorkflow.run
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, uuid.UUID(job_id))
    assert run_input == JobRunInput(
        run_id=str(run.id), engine="playwright", scrape=build_scrape_input(job, run, None)
    )
    assert call.kwargs["id"] == f"job-run-{run.id}"
    assert call.kwargs["task_queue"] == WORKFLOW_QUEUE
    assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE


async def test_a_workflow_that_cannot_start_fails_the_run_with_503(
    client, auth_headers, temporal, created_jobs
):
    temporal.start_workflow.side_effect = RuntimeError("engine down")
    response = await client.post("/jobs", json=PLAYWRIGHT_JOB, headers=auth_headers)

    assert response.status_code == 503
    job_id = response.json()["detail"]["job_id"]
    created_jobs.append(job_id)
    [run] = await _runs(job_id)
    assert (run.status, run.error) == ("failed", "could not start workflow: engine down")
    async with AsyncSessionLocal() as db:
        [delivery] = (
            await db.scalars(select(WebhookDelivery).where(WebhookDelivery.run_id == run.id))
        ).all()
    assert delivery.event == "job.failed"


async def test_the_scrape_input_carries_what_the_nats_message_carries(db_user):
    async with AsyncSessionLocal() as db:
        job = Job(
            user_id=db_user.id,
            url="https://example.com/x",
            engine="playwright",
            respect_robots=True,
            playwright_options={"wait_strategy": "load"},
            playwright_actions=[{"type": "wait", "milliseconds": 5}],
        )
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending", lane="v2")
        db.add(run)
        await db.commit()
        await db.refresh(job)
    credentials = {"encrypted_proxy_url": "p", "encrypted_cookies": "c"}

    message = build_scrape_message(job, run, credentials).model_dump(mode="json")
    scrape_input = build_scrape_input(job, run, credentials).model_dump(mode="json")

    # The activity contract drops the NATS-only fields (B.1); everything else is equal.
    for field in ("run_id", "engine", "crawl_context", "schema_version"):
        message.pop(field, None)
    assert scrape_input == message


async def _job_with_run(user_id, run_lane) -> tuple[Job, JobRun]:
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running", lane=run_lane)
        db.add(run)
        await db.commit()
    return job, run


@pytest.mark.parametrize("run_lane", ["v1", "v2"])
async def test_cancel_tells_the_engine_only_about_a_v2_run(
    client, auth_headers, temporal, run_lane
):
    await client.get("/users/me", headers=auth_headers)  # the mock user's row
    async with AsyncSessionLocal() as db:
        from app.models.user import User

        user = await db.scalar(select(User).where(User.clerk_id == "user_test_123"))
    job, run = await _job_with_run(user.id, run_lane)
    try:
        response = await client.delete(f"/jobs/{job.id}", headers=auth_headers)
        assert response.status_code == 200
        async with AsyncSessionLocal() as db:
            assert (await db.get(JobRun, run.id)).status == "cancelled"
        if run_lane == "v2":
            temporal.get_workflow_handle.assert_called_once_with(f"job-run-{run.id}")
            temporal.get_workflow_handle.return_value.cancel.assert_awaited_once()
        else:
            temporal.get_workflow_handle.assert_not_called()
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Job).where(Job.id == job.id))
            await db.commit()


async def test_a_cancel_still_lands_when_the_engine_is_down(client, auth_headers, temporal):
    temporal.get_workflow_handle.side_effect = RuntimeError("engine down")
    await client.get("/users/me", headers=auth_headers)
    async with AsyncSessionLocal() as db:
        from app.models.user import User

        user = await db.scalar(select(User).where(User.clerk_id == "user_test_123"))
    job, run = await _job_with_run(user.id, "v2")
    try:
        response = await client.delete(f"/jobs/{job.id}", headers=auth_headers)
        assert response.status_code == 200
        async with AsyncSessionLocal() as db:
            assert (await db.get(JobRun, run.id)).status == "cancelled"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Job).where(Job.id == job.id))
            await db.commit()


async def test_a_cancel_that_waits_on_a_finishing_run_leaves_it_finished(client, auth_headers):
    """A v2 activity holds the row lock while it completes the run; the cancel lands right after."""
    await client.get("/users/me", headers=auth_headers)
    async with AsyncSessionLocal() as db:
        from app.models.user import User

        user = await db.scalar(select(User).where(User.clerk_id == "user_test_123"))
    job, run = await _job_with_run(user.id, "v2")
    try:
        async with AsyncSessionLocal() as activity:
            row = await activity.scalar(select(JobRun).where(JobRun.id == run.id).with_for_update())
            row.status = "completed"
            await activity.flush()
            cancel = asyncio.create_task(client.delete(f"/jobs/{job.id}", headers=auth_headers))
            await asyncio.sleep(0.5)
            assert not cancel.done()  # waiting on the row lock
            await activity.commit()
        response = await cancel

        assert response.json()["message"] == "Job has no active run to cancel"
        async with AsyncSessionLocal() as db:
            assert (await db.get(JobRun, run.id)).status == "completed"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Job).where(Job.id == job.id))
            await db.commit()
