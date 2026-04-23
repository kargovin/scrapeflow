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


async def test_permanent_delete_calls_minio_remove_for_history_and_latest(
    client, auth_headers, mock_jetstream, mock_minio_client
):
    """MinIO remove_object is called for the history/ run object and the latest/ object."""
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com", "output_format": "html"},
        headers=auth_headers,
    )
    job_id = response.json()["id"]

    # Attach a result_path to the run so the permanent delete has a history/ object to remove.
    async with AsyncSessionLocal() as db:
        run = await db.scalar(select(JobRun).where(JobRun.job_id == uuid.UUID(job_id)))
        run.result_path = f"scrapeflow-results/history/{job_id}/1000000.html"
        run.status = "completed"
        await db.commit()

    await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)

    # One call per result_path (history/) + one call for latest/.
    assert mock_minio_client.remove_object.call_count == 2
    called_keys = {call.args[1] for call in mock_minio_client.remove_object.call_args_list}
    assert any("history/" in k for k in called_keys)
    assert any(k.startswith("latest/") for k in called_keys)


async def test_permanent_delete_markdown_uses_md_extension(
    client, auth_headers, mock_jetstream, mock_minio_client
):
    """latest/ object uses the .md extension for markdown output_format."""
    response = await client.post(
        "/jobs",
        json={"url": "https://example.com", "output_format": "markdown"},
        headers=auth_headers,
    )
    job_id = response.json()["id"]

    await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)

    latest_calls = [
        call
        for call in mock_minio_client.remove_object.call_args_list
        if call.args[1].startswith("latest/")
    ]
    assert len(latest_calls) == 1
    assert latest_calls[0].args[1].endswith(".md")


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
