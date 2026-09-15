"""Tests for DELETE /jobs/{id}?permanent=true (Step 28 — PRD-011)."""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal
from app.core.minio import get_minio
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.storage_object import StorageObject


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


@pytest.fixture
def mock_minio_client():
    """Override get_minio with an AsyncMock for the duration of the test."""
    mock = AsyncMock()
    app.dependency_overrides[get_minio] = lambda: mock
    yield mock
    app.dependency_overrides.pop(get_minio, None)


# ---------------------------------------------------------------------------
# Permanent delete — happy path
# ---------------------------------------------------------------------------


async def test_permanent_delete_returns_204(
    client, auth_headers, mock_jetstream, mock_minio_client
):
    """DELETE /jobs/{id}?permanent=true removes the job and returns 204."""
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com", "output_format": "html"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    response = await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)
    assert response.status_code == 204
    assert response.content == b""


async def test_permanent_delete_removes_db_row(
    client, auth_headers, mock_jetstream, mock_minio_client
):
    """Job row is absent from DB after permanent delete."""
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com"},
        headers=auth_headers,
    )
    job_id = response.json()["id"]

    await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)

    async with AsyncSessionLocal() as db:
        assert await db.get(Job, uuid.UUID(job_id)) is None


async def test_permanent_delete_removes_only_the_history_object(
    client, auth_headers, mock_jetstream, mock_minio_client
):
    """MinIO remove_object is called for the run's history/ object, and nothing else."""
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com", "output_format": "html"},
        headers=auth_headers,
    )
    job_id = response.json()["id"]

    # Record the run's object in the ledger so the permanent delete has something to
    # enumerate — the delete path reads rows, not result_path (P8).
    async with AsyncSessionLocal() as db:
        run = await db.scalar(select(JobRun).where(JobRun.job_id == uuid.UUID(job_id)))
        job = await db.get(Job, run.job_id)
        path = f"scrapeflow-results/history/{run.id}/scrape.html"
        run.result_path = path
        run.status = "completed"
        db.add(StorageObject(user_id=job.user_id, object_key=path, bytes=10, job_run_id=run.id))
        await db.commit()

    await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)

    # One call per ledger row. There is no second call for a latest/ mirror —
    # nothing writes one any more (ADR-011 §4).
    assert mock_minio_client.remove_object.call_count == 1
    called_keys = {call.args[1] for call in mock_minio_client.remove_object.call_args_list}
    assert all("history/" in k for k in called_keys)
    assert not any(k.startswith("latest/") for k in called_keys)


async def test_permanent_delete_never_reconstructs_a_key_from_output_format(
    client, auth_headers, mock_jetstream, mock_minio_client
):
    """
    The delete path used to rebuild `latest/{job_id}.{ext}` from job.output_format,
    which is the guessing half of BUG-007: an LLM job leaves objects under more than
    one extension, so a single reconstructed filename orphans the rest. With `latest/`
    gone the guess is gone too — a job with no stored result deletes no objects at all.
    """
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com", "output_format": "markdown"},
        headers=auth_headers,
    )
    job_id = response.json()["id"]

    await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)

    assert mock_minio_client.remove_object.call_count == 0


# ---------------------------------------------------------------------------
# Soft cancel still works (permanent=false default)
# ---------------------------------------------------------------------------


async def test_soft_cancel_returns_200_with_message(client, auth_headers, mock_jetstream):
    """DELETE /jobs/{id} without permanent param preserves the existing cancel behaviour."""
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com"},
        headers=auth_headers,
    )
    job_id = response.json()["id"]

    response = await client.delete(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    assert "cancel" in response.json()["message"].lower()

    # Job row still exists
    async with AsyncSessionLocal() as db:
        assert await db.get(Job, uuid.UUID(job_id)) is not None


# ---------------------------------------------------------------------------
# 404 for wrong-user and non-existent
# ---------------------------------------------------------------------------


async def test_permanent_delete_404_for_nonexistent_job(client, auth_headers, mock_minio_client):
    response = await client.delete(f"/jobs/{uuid.uuid4()}?permanent=true", headers=auth_headers)
    assert response.status_code == 404


async def test_permanent_delete_404_for_other_users_job(
    client, auth_headers, mock_jetstream, mock_minio_client, other_user
):
    """Permanent delete of another user's job returns 404 (not 403 — avoids resource leak)."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=other_user.id, url="https://example.com", output_format="html")
        db.add(job)
        await db.commit()
        job_id = str(job.id)

    try:
        response = await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)
        assert response.status_code == 404
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Job).where(Job.id == uuid.UUID(job_id)))
            await db.commit()
