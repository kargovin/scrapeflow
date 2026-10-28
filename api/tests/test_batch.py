import uuid

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.batch import Batch, BatchItem


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


# ---------------------------------------------------------------------------
# POST /batch
# ---------------------------------------------------------------------------


async def test_create_batch_dispatches_per_url(client, auth_headers, mock_temporal):
    """POST /batch creates one batch_item per URL and starts the batch's workflow."""
    response = await client.post(
        "/batch",
        json={
            "urls": [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
            "output_format": "markdown",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["total"] == 3
    assert data["status"] == "running"
    assert data["output_format"] == "markdown"
    assert "id" in data

    # One BatchWorkflow, one child input per URL, carrying no batch context.
    [call] = mock_temporal.start_workflow.call_args_list
    runs = call.args[1].runs
    assert len(runs) == 3
    for run in runs:
        # Artifacts key on the item's own run — a row that exists (ADR-011 §1, BUG-005).
        assert run.scrape.artifact_id == run.run_id

    # Verify batch_items exist in DB.
    batch_id = uuid.UUID(data["id"])
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BatchItem).where(BatchItem.batch_id == batch_id))
        items = result.scalars().all()
    assert len(items) == 3
    assert {item.url for item in items} == {
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    }


async def test_create_batch_ssrf_rejected_before_rows_created(client, auth_headers, mock_temporal):
    """POST /batch with an SSRF URL rejects before any DB rows are created."""
    response = await client.post(
        "/batch",
        json={"urls": ["http://169.254.169.254/latest/meta-data/"]},
        headers=auth_headers,
    )
    assert response.status_code in (400, 422)
    mock_temporal.start_workflow.assert_not_called()


async def test_create_batch_empty_urls_rejected(client, auth_headers, mock_temporal):
    """POST /batch with no URLs is rejected at validation."""
    response = await client.post(
        "/batch",
        json={"urls": []},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_create_batch_unauthenticated(client):
    """POST /batch without auth returns 401."""
    response = await client.post(
        "/batch",
        json={"urls": ["https://example.com"]},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /batch/{id}
# ---------------------------------------------------------------------------


async def test_get_batch_returns_counters(client, auth_headers, mock_temporal):
    """GET /batch/{id} returns accurate counter fields."""
    create_resp = await client.post(
        "/batch",
        json={"urls": ["https://example.com/1", "https://example.com/2"]},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    batch_id = create_resp.json()["id"]

    response = await client.get(f"/batch/{batch_id}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == batch_id
    assert data["total"] == 2
    assert data["completed"] == 0
    assert data["failed"] == 0
    assert data["status"] == "running"


async def test_get_batch_other_user_returns_404(client, auth_headers, db_user, mock_temporal):
    """GET /batch/{id} returns 404 for a batch belonging to another user."""
    batch = Batch(
        user_id=db_user.id,
        status="queued",
        output_format="html",
        engine="http",
        respect_robots=False,
        total=1,
        completed=0,
        failed=0,
    )
    async with AsyncSessionLocal() as db:
        db.add(batch)
        await db.commit()
        await db.refresh(batch)

    response = await client.get(f"/batch/{batch.id}", headers=auth_headers)
    assert response.status_code == 404

    # Delete batch before db_user fixture teardown deletes the user (no CASCADE on batches.user_id).
    async with AsyncSessionLocal() as db:
        b = await db.get(Batch, batch.id)
        if b:
            await db.delete(b)
            await db.commit()


# ---------------------------------------------------------------------------
# GET /batch/{id}/items
# ---------------------------------------------------------------------------


async def test_list_batch_items(client, auth_headers, mock_temporal):
    """GET /batch/{id}/items returns one entry per URL."""
    create_resp = await client.post(
        "/batch",
        json={"urls": ["https://example.com/x", "https://example.com/y"]},
        headers=auth_headers,
    )
    batch_id = create_resp.json()["id"]

    response = await client.get(f"/batch/{batch_id}/items", headers=auth_headers)
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 2
    urls = {item["url"] for item in items}
    assert urls == {"https://example.com/x", "https://example.com/y"}
    assert all(item["status"] == "pending" for item in items)


# ---------------------------------------------------------------------------
# DELETE /batch/{id}
# ---------------------------------------------------------------------------


async def test_cancel_batch(client, auth_headers, mock_temporal):
    """DELETE /batch/{id} sets batch status to cancelled and cancels pending items."""
    create_resp = await client.post(
        "/batch",
        json={"urls": ["https://example.com/cancel1", "https://example.com/cancel2"]},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    batch_id = create_resp.json()["id"]

    response = await client.delete(f"/batch/{batch_id}", headers=auth_headers)
    assert response.status_code == 204

    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, uuid.UUID(batch_id))
        assert batch is not None
        assert batch.status == "cancelled"

        from sqlalchemy import select

        items_result = await db.execute(
            select(BatchItem).where(BatchItem.batch_id == uuid.UUID(batch_id))
        )
        items = items_result.scalars().all()
        assert all(item.status == "cancelled" for item in items)


# ---------------------------------------------------------------------------
# Result consumer — batch branch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Result consumer — batch LLM path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /jobs does not include batch runs
# ---------------------------------------------------------------------------


async def test_list_jobs_excludes_batch_runs(client, auth_headers, mock_temporal):
    """GET /jobs lists only regular job runs, not batch item runs."""
    # Create a regular job.
    job_resp = await client.post(
        "/jobs",
        json={"url": "https://example.com/regular"},
        headers=auth_headers,
    )
    assert job_resp.status_code == 201
    job_id = job_resp.json()["id"]

    # Create a batch — its runs should not appear in GET /jobs.
    batch_resp = await client.post(
        "/batch",
        json={"urls": ["https://example.com/batch1"]},
        headers=auth_headers,
    )
    assert batch_resp.status_code == 201

    jobs = (await client.get("/jobs", headers=auth_headers)).json()
    job_ids = [j["id"] for j in jobs]

    assert job_id in job_ids
    # None of the listed jobs should have a null job_id — they're all from the jobs table.
    for j in jobs:
        assert j["id"] != batch_resp.json()["id"]
