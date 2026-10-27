"""F.4 — job, batch and crawl webhooks delivered by a workflow per row, on v1's wire contract."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from temporalio import activity
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from app.core import webhook_delivery
from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.batch import Batch
from app.models.crawl import Crawl
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities import webhook_deliveries
from app.workflows.activities.job_runs import finish_job_run
from app.workflows.activities.webhook_deliveries import (
    BACKOFF_SECONDS,
    attempt_webhook_delivery,
    sweep_pending_webhooks,
)
from app.workflows.job import FinishJobRunInput
from app.workflows.queues import WORKFLOW_QUEUE
from app.workflows.webhook_delivery import (
    ATTEMPT_WEBHOOK_ACTIVITY,
    AttemptOutcome,
    DeliveryRef,
    WebhookDeliveryWorkflow,
)
from tests.test_admin import admin_api_key, admin_headers, admin_user  # noqa: F401

_fernet = Fernet(settings.llm_key_encryption_key.encode())
SECRET = b"test-webhook-secret"
PAYLOAD = {
    "event": "job.completed",
    "job_id": "j",
    "run_id": "r",
    "result_path": None,
    "diff_detected": None,
    "diff_summary": None,
    "timestamp": "2026-04-12T00:00:00+00:00",
}


class Receiver:
    """Stands in for the POST; records exactly what would go on the wire."""

    def __init__(self, status=200, error=None):
        self.status, self.error = status, error
        self.posts: list[tuple[str, bytes, dict]] = []

    async def __call__(self, url, body, headers):
        self.posts.append((url, body, headers))
        if self.error:
            raise self.error
        return self.status


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setattr(webhook_deliveries, "validate_no_ssrf_core", lambda url: None)
    r = Receiver()
    monkeypatch.setattr(webhook_deliveries, "_post", r)
    return r


@pytest_asyncio.fixture
async def job(db_user):
    async with AsyncSessionLocal() as db:
        job = Job(
            user_id=db_user.id,
            url="https://example.com",
            webhook_url="https://hooks.example.com/in",
            webhook_secret=_fernet.encrypt(SECRET).decode(),
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


async def _delivery(job, attempts=0, status="pending", payload=PAYLOAD, **owner) -> WebhookDelivery:
    async with AsyncSessionLocal() as db:
        run = JobRun(job_id=job.id, status="completed", lane="v2")
        db.add(run)
        await db.flush()
        d = WebhookDelivery(
            job_id=owner.get("job_id", job.id) if not owner else owner.get("job_id"),
            batch_id=owner.get("batch_id"),
            crawl_id=owner.get("crawl_id"),
            run_id=None if owner.get("crawl_id") else run.id,
            event=payload["event"],
            webhook_url="https://hooks.example.com/in",
            payload=payload,
            status=status,
            attempts=attempts,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


async def _get(delivery_id) -> WebhookDelivery:
    async with AsyncSessionLocal() as db:
        return await db.get(WebhookDelivery, delivery_id)


def _attempt(d):
    return attempt_webhook_delivery(DeliveryRef(delivery_id=str(d.id)))


# ---------------------------------------------------------------------------
# One attempt — webhook_loop.py's, unchanged
# ---------------------------------------------------------------------------


async def test_a_2xx_marks_the_row_delivered(job, receiver):
    d = await _delivery(job)
    outcome = await _attempt(d)

    assert outcome.status == "delivered"
    row = await _get(d.id)
    assert (row.status, row.attempts) == ("delivered", 1)
    assert row.delivered_at is not None


async def test_the_wire_bytes_and_signature_are_the_job_lanes(job, receiver):
    d = await _delivery(job)
    await _attempt(d)

    [(url, body, headers)] = receiver.posts
    assert url == "https://hooks.example.com/in"
    # The stored row's payload as Postgres returns it (JSONB orders keys), as webhook_loop sent it.
    assert body == json.dumps((await _get(d.id)).payload).encode()
    assert json.loads(body) == PAYLOAD
    expected = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert headers == {
        "Content-Type": "application/json",
        "X-ScrapeFlow-Signature": f"sha256={expected}",
    }


async def test_a_5xx_counts_an_attempt_and_backs_off(job, receiver):
    receiver.status = 500
    d = await _delivery(job)

    outcome = await _attempt(d)

    row = await _get(d.id)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "HTTP 500")
    assert outcome.status == "retry"
    assert BACKOFF_SECONDS[1] - 2 <= outcome.retry_in_seconds <= BACKOFF_SECONDS[1]
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[1] - 5)


async def test_a_network_error_backs_off(job, receiver):
    receiver.error = httpx.ConnectError("refused")
    d = await _delivery(job)

    assert (await _attempt(d)).status == "retry"
    assert (await _get(d.id)).last_error == "refused"


async def test_the_last_attempt_exhausts_the_row(job, receiver):
    receiver.status = 503
    d = await _delivery(job, attempts=settings.webhook_max_attempts - 1)

    assert (await _attempt(d)).status == "exhausted"
    row = await _get(d.id)
    assert (row.status, row.attempts) == ("exhausted", settings.webhook_max_attempts)


async def test_an_ssrf_block_exhausts_without_counting_an_attempt(job, receiver, monkeypatch):
    def blocked(url):
        raise ValueError("resolves to a private address")

    monkeypatch.setattr(webhook_deliveries, "validate_no_ssrf_core", blocked)
    d = await _delivery(job)

    assert (await _attempt(d)).status == "exhausted"
    row = await _get(d.id)
    assert (row.status, row.attempts) == ("exhausted", 0)
    assert row.last_error.startswith("ssrf_blocked:")
    assert receiver.posts == []


async def test_a_row_no_longer_pending_is_not_posted(job, receiver):
    d = await _delivery(job, status="delivered")
    assert (await _attempt(d)).status == "gone"
    assert receiver.posts == []


async def test_a_batch_row_is_signed_with_the_batchs_secret(db_user, job, receiver):
    async with AsyncSessionLocal() as db:
        batch = Batch(
            user_id=db_user.id,
            total=1,
            output_format="html",
            webhook_url="https://hooks.example.com/in",
            webhook_secret=_fernet.encrypt(b"batch-secret").decode(),
        )
        db.add(batch)
        await db.commit()
    d = await _delivery(job, batch_id=batch.id, payload={"event": "batch.completed"})

    await _attempt(d)

    [(_, body, headers)] = receiver.posts
    expected = hmac.new(b"batch-secret", body, hashlib.sha256).hexdigest()
    assert headers["X-ScrapeFlow-Signature"] == f"sha256={expected}"


async def test_a_crawl_row_is_signed_with_an_empty_key(db_user, job, receiver):
    async with AsyncSessionLocal() as db:
        crawl = Crawl(
            user_id=db_user.id, seed_url="https://example.com", webhook_url="https://h.example"
        )
        db.add(crawl)
        await db.commit()
    d = await _delivery(job, crawl_id=crawl.id, payload={"event": "crawl.completed"})

    await _attempt(d)

    [(_, body, headers)] = receiver.posts
    expected = hmac.new(b"", body, hashlib.sha256).hexdigest()
    assert headers["X-ScrapeFlow-Signature"] == f"sha256={expected}"


# ---------------------------------------------------------------------------
# The workflow waits between attempts on the row's own schedule
# ---------------------------------------------------------------------------


async def _run_workflow(outcomes):
    seen = []

    @activity.defn(name=ATTEMPT_WEBHOOK_ACTIVITY)
    async def attempt(input: DeliveryRef) -> AttemptOutcome:
        seen.append(await env.get_current_time())
        return outcomes.pop(0)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[WebhookDeliveryWorkflow],
            activities=[attempt],
        ):
            result = await env.client.execute_workflow(
                WebhookDeliveryWorkflow.run,
                DeliveryRef(delivery_id=str(uuid.uuid4())),
                id=f"webhook-delivery-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
    return result, seen


async def test_the_workflow_retries_on_the_rows_backoff_until_delivered():
    result, seen = await _run_workflow(
        [
            AttemptOutcome(status="retry", retry_in_seconds=30),
            AttemptOutcome(status="retry", retry_in_seconds=300),
            AttemptOutcome(status="delivered"),
        ]
    )

    assert result == "delivered"
    gaps = [(b - a).total_seconds() for a, b in pairwise(seen)]
    assert 30 <= gaps[0] < 35
    assert 300 <= gaps[1] < 305


@pytest.mark.parametrize("final", ["exhausted", "gone"])
async def test_the_workflow_ends_on_a_final_outcome(final):
    result, seen = await _run_workflow([AttemptOutcome(status=final)])
    assert (result, len(seen)) == (final, 1)


# ---------------------------------------------------------------------------
# Starting deliveries
# ---------------------------------------------------------------------------


def _client():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    return client


async def test_a_delivery_is_started_by_row_id_and_never_twice_at_once():
    client = _client()
    delivery_id = uuid.uuid4()

    await webhook_delivery.start(client, [delivery_id])

    [call] = client.start_workflow.call_args_list
    assert call.args == (WebhookDeliveryWorkflow.run, DeliveryRef(delivery_id=str(delivery_id)))
    assert call.kwargs["id"] == f"webhook-delivery-{delivery_id}"
    assert call.kwargs["task_queue"] == WORKFLOW_QUEUE
    # One running for the row is joined; a closed one does not block a retry.
    assert call.kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE


async def test_finishing_a_run_starts_its_webhook_delivery(job):
    async with AsyncSessionLocal() as db:
        run = JobRun(job_id=job.id, status="running", lane="v2")
        db.add(run)
        await db.commit()
    client = _client()

    await ActivityEnvironment(client=client).run(
        finish_job_run, FinishJobRunInput(run_id=str(run.id), status="failed", error="boom")
    )

    async with AsyncSessionLocal() as db:
        [delivery] = (
            await db.scalars(select(WebhookDelivery).where(WebhookDelivery.run_id == run.id))
        ).all()
    [call] = client.start_workflow.call_args_list
    assert call.kwargs["id"] == f"webhook-delivery-{delivery.id}"


async def test_a_start_that_fails_leaves_the_row_for_the_sweep(job):
    async with AsyncSessionLocal() as db:
        run = JobRun(job_id=job.id, status="running", lane="v2")
        db.add(run)
        await db.commit()
    client = _client()
    client.start_workflow.side_effect = RuntimeError("temporal down")

    state = await ActivityEnvironment(client=client).run(
        finish_job_run, FinishJobRunInput(run_id=str(run.id), status="failed", error="boom")
    )

    assert state == "failed"
    async with AsyncSessionLocal() as db:
        [delivery] = (
            await db.scalars(select(WebhookDelivery).where(WebhookDelivery.run_id == run.id))
        ).all()
    assert delivery.status == "pending"


async def test_the_sweep_starts_overdue_pending_rows_only(job):
    overdue = await _delivery(job)
    fresh = await _delivery(job)
    done = await _delivery(job, status="delivered")
    async with AsyncSessionLocal() as db:
        (await db.get(WebhookDelivery, overdue.id)).next_attempt_at = datetime.now(UTC) - timedelta(
            minutes=5
        )
        await db.commit()
    client = _client()

    await ActivityEnvironment(client=client).run(sweep_pending_webhooks)

    started = {c.kwargs["id"] for c in client.start_workflow.call_args_list}
    assert f"webhook-delivery-{overdue.id}" in started
    assert f"webhook-delivery-{fresh.id}" not in started
    assert f"webhook-delivery-{done.id}" not in started


async def test_an_admin_retry_starts_the_delivery_again(
    client,
    admin_headers,  # noqa: F811
    job,
):
    d = await _delivery(job, status="exhausted", attempts=5)
    temporal = _client()
    app.state.temporal = temporal
    try:
        resp = await client.post(f"/admin/webhooks/deliveries/{d.id}/retry", headers=admin_headers)
    finally:
        app.state.temporal = None

    assert resp.status_code == 200
    row = await _get(d.id)
    assert (row.status, row.attempts) == ("pending", 0)
    [call] = temporal.start_workflow.call_args_list
    assert call.kwargs["id"] == f"webhook-delivery-{d.id}"


# ---------------------------------------------------------------------------
# F.5 — the three meters say what they count, and count a user's batches and crawls too
# ---------------------------------------------------------------------------


async def test_a_users_webhook_meters_count_their_batch_and_crawl_rows(
    client,
    admin_headers,  # noqa: F811
    db_user,
    job,
):
    from app.core.minio import get_minio
    from app.core.redis import get_redis
    from tests.test_admin import _make_mock_minio, _make_mock_redis

    async with AsyncSessionLocal() as db:
        batch = Batch(user_id=db_user.id, total=1, output_format="html")
        crawl = Crawl(user_id=db_user.id, seed_url="https://example.com")
        db.add_all([batch, crawl])
        await db.commit()
    await _delivery(job)
    await _delivery(job, batch_id=batch.id, payload={"event": "batch.completed"})
    await _delivery(
        job, crawl_id=crawl.id, payload={"event": "crawl.completed"}, status="exhausted"
    )
    await _delivery(job, status="delivered")

    app.dependency_overrides[get_redis] = lambda: _make_mock_redis()
    app.dependency_overrides[get_minio] = lambda: _make_mock_minio()
    try:
        resp = await client.get(f"/admin/stats/users/{db_user.id}", headers=admin_headers)
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_minio, None)

    data = resp.json()
    assert data["operational"]["job_lane_webhooks_pending"] == 2
    assert data["operational"]["job_lane_webhooks_exhausted"] == 1
    assert data["historical"]["job_lane_webhook_success_rate_7d"] == 0.25
    # The old names are gone: a reader cannot take them for every lane's.
    assert "webhook_deliveries_pending" not in data["operational"]
