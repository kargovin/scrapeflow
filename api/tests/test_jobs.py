import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import NATS_JOBS_RUN_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.result_consumer import _handle_result
from app.models.job import Job, JobStatus

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

    # Assert the NATS publish was called once with the correct subject and fat message
    mock_jetstream.publish.assert_called_once()
    call_subject, call_payload = mock_jetstream.publish.call_args.args
    assert call_subject == NATS_JOBS_RUN_SUBJECT
    published = json.loads(call_payload.decode())
    assert published["job_id"] == data["id"]
    assert published["url"] == "https://example.com/"
    assert published["output_format"] == "markdown"


# ---------------------------------------------------------------------------
# 6h-3: GET /jobs/{job_id} — own job returns 200
# ---------------------------------------------------------------------------


async def test_get_job_own(client, auth_headers, mock_jetstream):
    """GET /jobs/{job_id} returns 200 and the correct job for the owning user."""
    create_resp = await client.post(
        "/jobs",
        json={"url": "https://example.com"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["id"] == job_id


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


# ---------------------------------------------------------------------------
# 6h-6: DELETE /jobs/{job_id} — pending job transitions to cancelled
# ---------------------------------------------------------------------------


async def test_cancel_job(client, auth_headers, mock_jetstream):
    """DELETE /jobs/{job_id} sets a pending job to cancelled and returns it."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    response = await client.delete(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 6h-7: DELETE /jobs/{job_id} — already-cancelled is a no-op
# ---------------------------------------------------------------------------


async def test_cancel_job_noop(client, auth_headers, mock_jetstream):
    """DELETE /jobs/{job_id} on an already-cancelled job returns it unchanged."""
    create_resp = await client.post(
        "/jobs", json={"url": "https://example.com"}, headers=auth_headers
    )
    job_id = create_resp.json()["id"]

    # First cancel
    await client.delete(f"/jobs/{job_id}", headers=auth_headers)

    # Second cancel — should still return cancelled, not error
    response = await client.delete(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Result consumer tests (6h-8 to 6h-11)
# NOTE: mock_clerk_auth and db_user fixtures to be moved to conftest.py later
# ---------------------------------------------------------------------------


async def test_result_consumer_running(db_user):
    """Result consumer sets job status to running when worker publishes status=running."""
    # Insert a pending job directly in DB
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    # Build a fake NATS Msg with status=running
    msg = MagicMock()
    msg.data = json.dumps({"job_id": str(job_id), "status": "running"}).encode()
    msg.ack = AsyncMock()

    await _handle_result(msg)

    # Assert DB was updated
    async with AsyncSessionLocal() as db:
        updated = await db.get(Job, job_id)
        assert updated.status == JobStatus.running

    msg.ack.assert_called_once()


async def test_result_consumer_completed(db_user):
    """Result consumer sets job status to completed and saves result_path when worker publishes status=completed."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://other.com")
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    msg = MagicMock()
    msg.data = json.dumps(
        {
            "job_id": str(job_id),
            "status": "completed",
            "minio_path": f"scrapeflow-results/{job_id!s}.html",
        }
    ).encode()
    msg.ack = AsyncMock()

    await _handle_result(msg)

    async with AsyncSessionLocal() as db:
        updated = await db.get(Job, job_id)
        assert updated.status == JobStatus.completed
        assert updated.result_path == f"scrapeflow-results/{job_id!s}.html"

    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# 6h-11: Result consumer — cancelled job result is discarded
# ---------------------------------------------------------------------------


async def test_result_consumer_cancelled_job_discarded(db_user):
    """Result consumer discards worker results for cancelled jobs (status stays cancelled)."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com", status=JobStatus.cancelled)
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    # Worker sends a completed event — should be discarded
    msg = MagicMock()
    msg.data = json.dumps(
        {"job_id": str(job_id), "status": "completed", "minio_path": "some/path"}
    ).encode()
    msg.ack = AsyncMock()

    await _handle_result(msg)

    async with AsyncSessionLocal() as db:
        updated = await db.get(Job, job_id)
        assert updated.status == JobStatus.cancelled  # unchanged
        assert updated.result_path is None  # not written

    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# 6h-10: Result consumer failed event → DB status + error updated
# ---------------------------------------------------------------------------


async def test_result_consumer_failed(db_user):
    """Result consumer sets job status to failed and saves error when worker publishes status=failed."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    msg = MagicMock()
    msg.data = json.dumps(
        {"job_id": str(job_id), "status": "failed", "error": "connection timeout"}
    ).encode()
    msg.ack = AsyncMock()

    await _handle_result(msg)

    async with AsyncSessionLocal() as db:
        updated = await db.get(Job, job_id)
        assert updated.status == JobStatus.failed
        assert updated.error == "connection timeout"

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
    with patch("app.routers.jobs.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [(None, None, None, None, (resolved_ip, 0))]
        response = await client.post("/jobs", json={"url": url}, headers=auth_headers)
    assert response.status_code == 400
    assert "private" in response.json()["detail"]


async def test_create_job_ssrf_unresolvable(client, auth_headers):
    """Hostnames that fail DNS resolution are rejected with 422."""
    import socket as _socket

    with patch(
        "app.routers.jobs.socket.getaddrinfo", side_effect=_socket.gaierror("name not found")
    ):
        response = await client.post(
            "/jobs", json={"url": "http://nosuchodomain.invalid/"}, headers=auth_headers
        )
    assert response.status_code == 422
    assert "resolved" in response.json()["detail"]


async def test_create_job_ssrf_public_url_allowed(client, auth_headers, mock_jetstream):
    """Public IPs pass SSRF check and proceed to job creation."""
    with patch("app.routers.jobs.socket.getaddrinfo") as mock_gai:
        mock_gai.return_value = [(None, None, None, None, ("93.184.216.34", 0))]  # example.com
        response = await client.post(
            "/jobs", json={"url": "https://example.com"}, headers=auth_headers
        )
    assert response.status_code == 201
