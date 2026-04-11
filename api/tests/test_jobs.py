import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, select

from app.constants import NATS_JOBS_LLM_SUBJECT, NATS_JOBS_RUN_HTTP_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.result_consumer import _handle_result
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_headers(mock_clerk_auth):
    """Return auth headers for the default mock Clerk user."""
    return {"Authorization": "Bearer fake.jwt.token"}


# ---------------------------------------------------------------------------
# 6h-1: unauthenticated requests return 401 on all job endpoints
# ---------------------------------------------------------------------------


async def test_jobs_unauthenticated(client):
    """All job endpoints return 401 when no auth header is provided."""
    fake_id = uuid.uuid4()
    assert (await client.post("/jobs", json={"url": "https://example.com"})).status_code == 401
    assert (await client.get("/jobs")).status_code == 401
    assert (await client.get(f"/jobs/{fake_id}")).status_code == 401
    assert (await client.delete(f"/jobs/{fake_id}")).status_code == 401


# ---------------------------------------------------------------------------
# 6h-2: POST /jobs creates a job and publishes to NATS
# ---------------------------------------------------------------------------


async def test_create_job(client, auth_headers, mock_jetstream):
    """POST /jobs returns 201, correct fields, and publishes a fat NATS message."""

    response = await client.post(
        "/jobs",
        json={"url": "https://example.com", "output_format": "markdown"},
        headers=auth_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["url"] == "https://example.com/"  # AnyHttpUrl normalises trailing slash
    assert data["output_format"] == "markdown"
    assert data["status"] == "pending"
    assert "id" in data
    assert "run_id" in data

    # Assert the NATS publish was called once with the correct subject and fat message
    mock_jetstream.publish.assert_called_once()
    call_subject, call_payload = mock_jetstream.publish.call_args.args
    assert call_subject == NATS_JOBS_RUN_HTTP_SUBJECT
    published = json.loads(call_payload.decode())
    assert published["job_id"] == data["id"]
    assert published["url"] == "https://example.com/"
    assert published["output_format"] == "markdown"
    assert published["run_id"] == data["run_id"]


# ---------------------------------------------------------------------------
# 6h-3: GET /jobs/{job_id} — own job returns 200
# ---------------------------------------------------------------------------


async def test_get_job_own(client, auth_headers, mock_jetstream):
    """GET /jobs/{job_id} returns 200 with run_id and status sourced from job_runs."""
    create_resp = await client.post(
        "/jobs",
        json={"url": "https://example.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    data = create_resp.json()
    job_id = data["id"]

    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job_id
    assert body["run_id"] == data["run_id"]
    assert body["status"] == "pending"


async def test_get_job_status_from_run(client, auth_headers, mock_jetstream):
    """GET /jobs/{job_id} reflects status from job_runs, not jobs."""
    create_resp = await client.post(
        "/jobs",
        json={"url": "https://example.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    run_id = uuid.UUID(create_resp.json()["run_id"])
    job_id = create_resp.json()["id"]

    # Simulate worker updating the run to running
    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        assert run is not None
        run.status = "running"
        await db.commit()

    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "running"


# ---------------------------------------------------------------------------
# 6h-4: GET /jobs/{job_id} — another user's job returns 404
# ---------------------------------------------------------------------------


async def test_get_job_other_user(client, auth_headers, db_user):
    """GET /jobs/{job_id} returns 404 when the job belongs to a different user."""
    job = Job(user_id=db_user.id, url="https://other.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)

    response = await client.get(f"/jobs/{job.id}", headers=auth_headers)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 6h-5: GET /jobs — returns only current user's jobs, respects limit/offset
# ---------------------------------------------------------------------------


async def test_list_jobs_pagination(client, auth_headers, mock_jetstream):
    """GET /jobs returns only the authenticated user's jobs and respects limit/offset."""
    # Create 3 jobs as the mock user
    for _ in range(3):
        resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=auth_headers)
        assert resp.status_code == 201

    # limit=2 must return exactly 2 items
    resp = await client.get("/jobs?limit=2", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # offset beyond total returns empty list
    resp = await client.get("/jobs?limit=200&offset=9999", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # each item must include run_id
    resp = await client.get("/jobs", headers=auth_headers)
    assert all("run_id" in job for job in resp.json())


# ---------------------------------------------------------------------------
# 6h-6: DELETE /jobs/{job_id} — pending job transitions to cancelled
# ---------------------------------------------------------------------------


async def test_cancel_job(client, auth_headers, mock_jetstream):
    """DELETE /jobs/{job_id} cancels the active job_run and returns a message."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    response = await client.delete(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["message"] == "Job run cancelled"


# ---------------------------------------------------------------------------
# 6h-7: DELETE /jobs/{job_id} — no active run returns appropriate message
# ---------------------------------------------------------------------------


async def test_cancel_job_noop(client, auth_headers, mock_jetstream):
    """DELETE /jobs/{job_id} with no active run returns 'no active run' message."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    job_id = create_resp.json()["id"]

    # First cancel — cancels the active run
    await client.delete(f"/jobs/{job_id}", headers=auth_headers)

    # Second cancel — no active run remains
    response = await client.delete(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["message"] == "Job has no active run to cancel"


async def test_cancel_job_terminal_run(client, auth_headers, mock_jetstream):
    """DELETE /jobs/{job_id} returns 'no active run' when the run is already completed."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    run_id = uuid.UUID(create_resp.json()["run_id"])

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        assert run is not None
        run.status = "completed"
        await db.commit()

    response = await client.delete(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["message"] == "Job has no active run to cancel"


# ---------------------------------------------------------------------------
# Result consumer tests (Phase 2 — Steps 6h-8 to 6h-11)
# ---------------------------------------------------------------------------


async def test_result_consumer_running(db_user):
    """Result consumer sets run.status=running, started_at from NATS timestamp, and nats_stream_seq."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending")
        db.add(run)
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    mock_ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    msg = MagicMock()
    msg.data = json.dumps(
        {"job_id": str(job_id), "run_id": str(run_id), "status": "running", "nats_stream_seq": 42}
    ).encode()
    msg.metadata = MagicMock()
    msg.metadata.timestamp = mock_ts
    msg.ack = AsyncMock()

    await _handle_result(msg, AsyncMock(), MagicMock())

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "running"
        assert updated_run.started_at == mock_ts
        assert updated_run.nats_stream_seq == 42

    msg.ack.assert_called_once()


async def test_result_consumer_completed(db_user):
    """Result consumer marks run completed, sets result_path/completed_at, creates webhook delivery (no LLM, no prior run)."""
    async with AsyncSessionLocal() as db:
        job = Job(
            user_id=db_user.id,
            url="https://example.com",
            webhook_url="https://hook.example.com",
        )
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    minio_path = f"scrapeflow-results/history/{job_id}/1234567890.html"
    msg = MagicMock()
    msg.data = json.dumps(
        {
            "job_id": str(job_id),
            "run_id": str(run_id),
            "status": "completed",
            "minio_path": minio_path,
        }
    ).encode()
    msg.metadata = MagicMock()
    msg.ack = AsyncMock()

    await _handle_result(msg, AsyncMock(), MagicMock())

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "completed"
        assert updated_run.result_path == minio_path
        assert updated_run.completed_at is not None
        assert updated_run.diff_detected is None  # no previous run to diff against

        delivery_result = await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.run_id == run_id)
        )
        delivery = delivery_result.scalar_one_or_none()
        assert delivery is not None
        assert delivery.payload["event"] == "job.completed"
        assert delivery.status == "pending"

    msg.ack.assert_called_once()


async def test_result_consumer_completed_with_llm(db_user):
    """Result consumer routes to LLM worker when job.llm_config is set (run transitions to processing)."""
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(
            user_id=db_user.id,
            name="test-key",
            provider="anthropic",
            encrypted_api_key="enc_key",
        )
        db.add(key)
        await db.flush()
        job = Job(
            user_id=db_user.id,
            url="https://example.com",
            llm_config={"llm_key_id": str(key.id), "model": "claude-3", "output_schema": {}},
        )
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        await db.commit()
        await db.refresh(key)
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    minio_path = f"scrapeflow-results/history/{job_id}/1234567890.html"
    msg = MagicMock()
    msg.data = json.dumps(
        {
            "job_id": str(job_id),
            "run_id": str(run_id),
            "status": "completed",
            "minio_path": minio_path,
        }
    ).encode()
    msg.metadata = MagicMock()
    msg.ack = AsyncMock()

    mock_js = AsyncMock()
    await _handle_result(msg, mock_js, MagicMock())

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "processing"

    mock_js.publish.assert_called_once()
    subject, payload_bytes = mock_js.publish.call_args.args
    assert subject == NATS_JOBS_LLM_SUBJECT
    published = json.loads(payload_bytes.decode())
    assert published["job_id"] == str(job_id)
    assert published["run_id"] == str(run_id)
    assert published["raw_minio_path"] == minio_path

    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# 6h-11: Result consumer — cancelled run result is discarded
# ---------------------------------------------------------------------------


async def test_result_consumer_cancelled_job_discarded(db_user):
    """Result consumer discards worker results for cancelled runs (run status stays cancelled)."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="cancelled")
        db.add(run)
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    msg = MagicMock()
    msg.data = json.dumps(
        {
            "job_id": str(job_id),
            "run_id": str(run_id),
            "status": "completed",
            "minio_path": "some/path",
        }
    ).encode()
    msg.metadata = MagicMock()
    msg.ack = AsyncMock()

    await _handle_result(msg, AsyncMock(), MagicMock())

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "cancelled"  # unchanged
        assert updated_run.result_path is None  # not written

    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# 6h-10: Result consumer failed event → run status + error + webhook delivery
# ---------------------------------------------------------------------------


async def test_result_consumer_failed(db_user):
    """Result consumer sets run.status=failed, error, completed_at, and creates webhook delivery."""
    async with AsyncSessionLocal() as db:
        job = Job(
            user_id=db_user.id,
            url="https://example.com",
            webhook_url="https://hook.example.com",
        )
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    msg = MagicMock()
    msg.data = json.dumps(
        {
            "job_id": str(job_id),
            "run_id": str(run_id),
            "status": "failed",
            "error": "connection timeout",
        }
    ).encode()
    msg.metadata = MagicMock()
    msg.ack = AsyncMock()

    await _handle_result(msg, AsyncMock(), MagicMock())

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "failed"
        assert updated_run.error == "connection timeout"
        assert updated_run.completed_at is not None

        delivery_result = await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.run_id == run_id)
        )
        delivery = delivery_result.scalar_one_or_none()
        assert delivery is not None
        assert delivery.payload["event"] == "job.failed"
        assert delivery.payload["error"] == "connection timeout"
        assert delivery.status == "pending"

    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,resolved_ip",
    [
        ("http://localhost/secret", "127.0.0.1"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("http://redis/data", "10.0.0.1"),
        ("http://192.168.1.1/admin", "192.168.1.1"),
    ],
)
async def test_create_job_ssrf_blocked(client, auth_headers, url, resolved_ip):
    """URLs resolving to private/loopback/link-local addresses are rejected with 400."""
    with patch("app.core.security.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [(None, None, None, None, (resolved_ip, 0))]
        response = await client.post("/jobs", json={"url": url}, headers=auth_headers)
    assert response.status_code == 400
    assert "private" in response.json()["detail"]


async def test_create_job_ssrf_unresolvable(client, auth_headers):
    """Hostnames that fail DNS resolution are rejected with 422."""
    import socket as _socket

    with patch(
        "app.core.security.socket.getaddrinfo", side_effect=_socket.gaierror("name not found")
    ):
        response = await client.post(
            "/jobs", json={"url": "http://nosuchodomain.invalid/"}, headers=auth_headers
        )
    assert response.status_code == 422
    assert "resolved" in response.json()["detail"]


async def test_create_job_ssrf_public_url_allowed(client, auth_headers, mock_jetstream):
    """Public IPs pass SSRF check and proceed to job creation."""
    with patch("app.core.security.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [(None, None, None, None, ("93.184.216.34", 0))]  # example.com
        response = await client.post(
            "/jobs", json={"url": "https://example.com"}, headers=auth_headers
        )
    assert response.status_code == 201


# Cron validation unit test
async def test_create_job_valid_cron_job(client, auth_headers, mock_jetstream):
    response = await client.post(
        "/jobs",
        json={"url": "https://domain.com", "schedule_cron": "0 * * * *"},
        headers=auth_headers,
    )
    assert response.status_code == 201


async def test_create_job_invalid_cron_job(client, auth_headers, mock_jetstream):
    response = await client.post(
        "/jobs",
        json={"url": "https://domain.com", "schedule_cron": "65 * * * *"},
        headers=auth_headers,
    )
    assert response.status_code == 422


# LLM key unit tests
async def test_create_job_valid_llm_key(client, auth_headers, mock_jetstream):
    # Create and add a key to llm table
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.clerk_id == "user_test_123"))
        auth_user = res.scalar_one()

        key = UserLLMKey(
            user_id=auth_user.id, name="test", provider="anthropic", encrypted_api_key="testnow"
        )
        db.add(key)
        await db.commit()
        await db.refresh(key)

    response = await client.post(
        "/jobs",
        json={
            "url": "https://domain.com",
            "llm_config": {"llm_key_id": str(key.id), "model": "abc", "output_schema": {}},
        },
        headers=auth_headers,
    )
    assert response.status_code == 201


async def test_create_job_invalid_llmkey(client, auth_headers, mock_jetstream):
    response = await client.post(
        "/jobs",
        json={
            "url": "https://domain.com",
            "llm_config": {
                "llm_key_id": "00000000-0000-0000-0000-000000000000",
                "model": "abc",
                "output_schema": {},
            },
        },
        headers=auth_headers,
    )
    assert response.status_code == 404


# webhook url validation
async def test_create_job_valid_webhook_returns_secret(client, auth_headers, mock_jetstream):
    response = await client.post(
        "/jobs",
        json={"url": "https://domain.com", "webhook_url": "https://anotherdomain.com"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["webhook_secret"] is not None


# Job run creation
async def test_create_job_job_runs_entry_exists(client, auth_headers, mock_jetstream):
    response = await client.post("/jobs", json={"url": "https://domain.com"}, headers=auth_headers)

    assert response.status_code == 201
    run_id = uuid.UUID(response.json()["run_id"])

    async with AsyncSessionLocal() as db:
        res: JobRun | None = await db.get(JobRun, run_id)
        assert res is not None
        assert res.status == "pending"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/runs — paginated run history
# ---------------------------------------------------------------------------


async def test_list_job_runs(client, auth_headers, mock_jetstream):
    """GET /jobs/{id}/runs returns all runs for a job the user owns."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    run_id = create_resp.json()["run_id"]

    # Add a second run directly in DB
    async with AsyncSessionLocal() as db:
        db.add(JobRun(job_id=uuid.UUID(job_id), status="completed"))
        await db.commit()

    resp = await client.get(f"/jobs/{job_id}/runs", headers=auth_headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 2
    assert any(r["run_id"] == run_id for r in runs)


async def test_list_job_runs_other_user(client, auth_headers, db_user):
    """GET /jobs/{id}/runs returns 404 for a job belonging to another user."""
    job = Job(user_id=db_user.id, url="https://other.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)

    resp = await client.get(f"/jobs/{job.id}/runs", headers=auth_headers)
    assert resp.status_code == 404


async def test_list_job_runs_empty(client, auth_headers, mock_jetstream):
    """GET /jobs/{id}/runs returns [] for a job with no runs."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    run_id = uuid.UUID(create_resp.json()["run_id"])

    # Delete the auto-created run to simulate a job with no run history
    async with AsyncSessionLocal() as db:
        await db.execute(delete(JobRun).where(JobRun.id == run_id))
        await db.commit()

    resp = await client.get(f"/jobs/{job_id}/runs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_job_runs_pagination(client, auth_headers, mock_jetstream):
    """GET /jobs/{id}/runs respects limit and offset."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    # Add 2 more runs — 3 total
    async with AsyncSessionLocal() as db:
        for _ in range(2):
            db.add(JobRun(job_id=uuid.UUID(job_id), status="completed"))
        await db.commit()

    resp = await client.get(f"/jobs/{job_id}/runs?limit=2", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await client.get(f"/jobs/{job_id}/runs?limit=10&offset=2", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# PATCH /jobs/{id} — partial update
# ---------------------------------------------------------------------------


async def test_patch_job_schedule_cron(client, auth_headers, mock_jetstream):
    """PATCH /jobs/{id} with schedule_cron updates cron and recalculates next_run_at."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_cron": "0 * * * *"}, headers=auth_headers
    )
    assert resp.status_code == 200

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        assert job is not None
        assert job.schedule_cron == "0 * * * *"
        assert job.next_run_at is not None


async def test_patch_job_invalid_cron(client, auth_headers, mock_jetstream):
    """PATCH /jobs/{id} with an invalid cron expression returns 422."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/jobs/{job_id}", json={"schedule_cron": "65 * * * *"}, headers=auth_headers
    )
    assert resp.status_code == 422


async def test_patch_job_immutable_fields(client, auth_headers, mock_jetstream):
    """PATCH /jobs/{id} rejects immutable fields (url, engine, output_format) with 422."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    for immutable_payload in [
        {"url": "https://changed.com"},
        {"engine": "playwright"},
        {"output_format": "json"},
    ]:
        resp = await client.patch(f"/jobs/{job_id}", json=immutable_payload, headers=auth_headers)
        assert resp.status_code == 422, f"Expected 422 for payload {immutable_payload}"


async def test_patch_job_webhook_url_set(client, auth_headers, mock_jetstream):
    """PATCH /jobs/{id} setting webhook_url generates and returns the new secret."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/jobs/{job_id}",
        json={"webhook_url": "https://anotherdomain.com"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["webhook_secret"] is not None


async def test_patch_job_webhook_url_removed(client, auth_headers, mock_jetstream):
    """PATCH /jobs/{id} with webhook_url=null clears the webhook URL and secret."""
    create_resp = await client.post(
        "/jobs",
        json={"url": "https://example.com", "webhook_url": "https://anotherdomain.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    resp = await client.patch(f"/jobs/{job_id}", json={"webhook_url": None}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["webhook_secret"] is None

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        assert job is not None
        assert job.webhook_url is None
        assert job.webhook_secret is None


async def test_patch_job_llm_config_409(client, auth_headers, mock_jetstream):
    """PATCH /jobs/{id} with llm_config returns 409 if the latest run is processing."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    run_id = uuid.UUID(create_resp.json()["run_id"])

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        assert run is not None
        run.status = "processing"
        await db.commit()

        res = await db.execute(select(User).where(User.clerk_id == "user_test_123"))
        auth_user = res.scalar_one()
        key = UserLLMKey(
            user_id=auth_user.id, name="test", provider="anthropic", encrypted_api_key="testkey"
        )
        db.add(key)
        await db.commit()
        await db.refresh(key)

    resp = await client.patch(
        f"/jobs/{job_id}",
        json={"llm_config": {"llm_key_id": str(key.id), "model": "claude-3", "output_schema": {}}},
        headers=auth_headers,
    )
    assert resp.status_code == 409


async def test_patch_job_other_user(client, auth_headers, db_user):
    """PATCH /jobs/{id} returns 404 for a job belonging to another user."""
    job = Job(user_id=db_user.id, url="https://other.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)

    resp = await client.patch(
        f"/jobs/{job.id}", json={"schedule_cron": "0 * * * *"}, headers=auth_headers
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /jobs/{id}/webhook-secret/rotate
# ---------------------------------------------------------------------------


async def test_rotate_webhook_secret(client, auth_headers, mock_jetstream):
    """POST .../rotate returns new plaintext secret and updates the encrypted secret in DB."""
    create_resp = await client.post(
        "/jobs",
        json={"url": "https://example.com", "webhook_url": "https://anotherdomain.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        assert job is not None
        original_encrypted = job.webhook_secret

    resp = await client.post(f"/jobs/{job_id}/webhook-secret/rotate", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "webhook_secret" in data
    assert data["webhook_secret"] is not None

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        assert job is not None
        assert job.webhook_secret != original_encrypted


async def test_rotate_webhook_secret_no_webhook(client, auth_headers, mock_jetstream):
    """POST .../rotate returns 422 for a job that has no webhook URL."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    resp = await client.post(f"/jobs/{job_id}/webhook-secret/rotate", headers=auth_headers)
    assert resp.status_code == 422


async def test_rotate_webhook_secret_other_user(client, auth_headers, db_user):
    """POST .../rotate returns 404 for a job belonging to another user."""
    job = Job(user_id=db_user.id, url="https://other.com", webhook_url="https://hook.example.com")
    async with AsyncSessionLocal() as db:
        db.add(job)
        await db.commit()
        await db.refresh(job)

    resp = await client.post(f"/jobs/{job.id}/webhook-secret/rotate", headers=auth_headers)
    assert resp.status_code == 404
