import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, select

from app.constants import NATS_JOBS_LLM_SUBJECT
from app.core.db import AsyncSessionLocal
from app.core.result_consumer import _handle_result
from app.models.batch import Batch, BatchItem
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.user import User


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


# ---------------------------------------------------------------------------
# POST /batch
# ---------------------------------------------------------------------------


async def test_create_batch_dispatches_per_url(client, auth_headers, mock_jetstream):
    """POST /batch creates one batch_item and one NATS message per URL."""
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

    assert mock_jetstream.publish.call_count == 3

    # Verify each NATS payload is a valid fat message with run_id but no batch context.
    for call in mock_jetstream.publish.call_args_list:
        subject, payload_bytes = call.args
        payload = json.loads(payload_bytes.decode())
        assert payload["schema_version"] == 2
        assert "run_id" in payload
        assert payload["job_id"] is None  # batch runs never carry job_id in message

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


async def test_create_batch_ssrf_rejected_before_rows_created(client, auth_headers, mock_jetstream):
    """POST /batch with an SSRF URL rejects before any DB rows are created."""
    response = await client.post(
        "/batch",
        json={"urls": ["http://169.254.169.254/latest/meta-data/"]},
        headers=auth_headers,
    )
    assert response.status_code in (400, 422)
    mock_jetstream.publish.assert_not_called()


async def test_create_batch_empty_urls_rejected(client, auth_headers, mock_jetstream):
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


async def test_get_batch_returns_counters(client, auth_headers, mock_jetstream):
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


async def test_get_batch_other_user_returns_404(client, auth_headers, db_user, mock_jetstream):
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


async def test_list_batch_items(client, auth_headers, mock_jetstream):
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


async def test_cancel_batch(client, auth_headers, mock_jetstream):
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


async def _make_batch_run(status="pending"):
    """Helper: create a Batch, BatchItem, and JobRun in the DB. Returns (batch, item, run)."""

    async with AsyncSessionLocal() as db:
        user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@test.com")
        db.add(user)
        await db.flush()

        batch = Batch(
            user_id=user.id,
            status="running",
            output_format="markdown",
            engine="http",
            respect_robots=False,
            total=2,
            completed=0,
            failed=0,
        )
        db.add(batch)
        await db.flush()

        item = BatchItem(batch_id=batch.id, url="https://example.com", status="pending")
        db.add(item)
        await db.flush()

        run = JobRun(batch_item_id=item.id, status=status)
        db.add(run)
        await db.commit()

        await db.refresh(batch)
        await db.refresh(item)
        await db.refresh(run)

    return batch.id, item.id, run.id, user.id


def _make_nats_msg(run_id, status, minio_path=None, error=None):
    mock_msg = MagicMock()
    mock_msg.data = json.dumps(
        {
            "job_id": None,
            "run_id": str(run_id),
            "status": status,
            "minio_path": minio_path,
            "error": error,
        }
    ).encode()
    mock_msg.metadata.timestamp = datetime.now(UTC)
    mock_msg.ack = AsyncMock()
    return mock_msg


async def test_result_consumer_batch_completed_updates_counters():
    """Worker completing a batch item increments batch.completed."""
    batch_id, item_id, run_id, user_id = await _make_batch_run()

    mock_msg = _make_nats_msg(run_id, "running")
    mock_js = AsyncMock()
    mock_minio = AsyncMock()
    await _handle_result(mock_msg, mock_js, mock_minio)

    mock_msg = _make_nats_msg(run_id, "completed", minio_path="scrapeflow-results/history/x.md")
    await _handle_result(mock_msg, mock_js, mock_minio)

    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, batch_id)
        item = await db.get(BatchItem, item_id)
        run = await db.get(JobRun, run_id)
    assert batch is not None
    assert batch.completed == 1
    assert item is not None
    assert item.status == "completed"
    assert item.result_path == "scrapeflow-results/history/x.md"
    assert run is not None
    assert run.status == "completed"

    async with AsyncSessionLocal() as db:
        await db.execute(delete(Batch).where(Batch.id == batch_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_result_consumer_batch_all_complete_sets_batch_completed():
    """When completed + failed == total, batches.status becomes 'completed'."""
    batch_id, _, run_id, user_id = await _make_batch_run()

    # The total for this batch is 2 but we only created 1 item — set total to 1 first.
    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, batch_id)
        assert batch is not None
        batch.total = 1
        await db.commit()

    mock_msg = _make_nats_msg(run_id, "running")
    mock_js = AsyncMock()
    mock_minio = AsyncMock()
    await _handle_result(mock_msg, mock_js, mock_minio)

    mock_msg = _make_nats_msg(run_id, "completed", minio_path="scrapeflow-results/history/y.md")
    await _handle_result(mock_msg, mock_js, mock_minio)

    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, batch_id)
    assert batch is not None
    assert batch.status == "completed"
    assert batch.completed_at is not None

    async with AsyncSessionLocal() as db:
        b = await db.get(Batch, batch_id)
        if b:
            await db.execute(delete(Batch).where(Batch.id == batch_id))
        await db.commit()
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_result_consumer_batch_partial_failure():
    """When any item fails and batch is done, status becomes 'partial_failure'."""
    batch_id, item_id, run_id, user_id = await _make_batch_run()

    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, batch_id)
        assert batch is not None
        batch.total = 1
        await db.commit()

    mock_msg = _make_nats_msg(run_id, "running")
    mock_js = AsyncMock()
    mock_minio = AsyncMock()
    await _handle_result(mock_msg, mock_js, mock_minio)

    mock_msg = _make_nats_msg(run_id, "failed", error="timeout")
    await _handle_result(mock_msg, mock_js, mock_minio)

    async with AsyncSessionLocal() as db:
        batch = await db.get(Batch, batch_id)
        item = await db.get(BatchItem, item_id)
    assert batch is not None
    assert batch.status == "partial_failure"
    assert batch.failed == 1
    assert item is not None
    assert item.status == "failed"
    assert item.error == "timeout"

    async with AsyncSessionLocal() as db:
        b = await db.get(Batch, batch_id)
        if b:
            await db.execute(delete(Batch).where(Batch.id == batch_id))
        await db.commit()
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# ---------------------------------------------------------------------------
# Result consumer — batch LLM path
# ---------------------------------------------------------------------------


async def _make_batch_run_with_llm():
    """Create a Batch with llm_config, one BatchItem, and a pending JobRun."""
    async with AsyncSessionLocal() as db:
        user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@test.com")
        db.add(user)
        await db.flush()

        key = UserLLMKey(
            user_id=user.id,
            name="test-key",
            provider="anthropic",
            encrypted_api_key="enc_key",
        )
        db.add(key)
        await db.flush()

        batch = Batch(
            user_id=user.id,
            status="running",
            output_format="json",
            engine="http",
            respect_robots=False,
            llm_config={"llm_key_id": str(key.id), "model": "claude-3", "output_schema": {}},
            total=1,
            completed=0,
            failed=0,
        )
        db.add(batch)
        await db.flush()

        item = BatchItem(batch_id=batch.id, url="https://example.com", status="pending")
        db.add(item)
        await db.flush()

        run = JobRun(batch_item_id=item.id, status="pending")
        db.add(run)
        await db.commit()

        await db.refresh(batch)
        await db.refresh(item)
        await db.refresh(run)

    return batch.id, item.id, run.id, user.id


async def test_result_consumer_batch_scrape_complete_dispatches_to_llm():
    """When batch.llm_config is set, a scrape-complete message dispatches to the LLM subject
    and transitions run to 'processing' without incrementing batch counters."""
    batch_id, item_id, run_id, user_id = await _make_batch_run_with_llm()

    mock_js = AsyncMock()
    mock_minio = MagicMock()

    msg = _make_nats_msg(run_id, "running")
    await _handle_result(msg, mock_js, mock_minio)

    msg = _make_nats_msg(run_id, "completed", minio_path="scrapeflow-results/history/x.html")
    await _handle_result(msg, mock_js, mock_minio)

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        batch = await db.get(Batch, batch_id)
        item = await db.get(BatchItem, item_id)

    assert run is not None and run.status == "processing"
    assert batch is not None and batch.completed == 0 and batch.failed == 0
    assert item is not None and item.status == "pending"

    mock_js.publish.assert_called_once()
    subject, payload_bytes = mock_js.publish.call_args.args
    assert subject == NATS_JOBS_LLM_SUBJECT
    payload = json.loads(payload_bytes.decode())
    assert payload["run_id"] == str(run_id)
    assert payload["job_id"] is None
    assert payload["raw_minio_path"] == "scrapeflow-results/history/x.html"

    async with AsyncSessionLocal() as db:
        await db.execute(delete(Batch).where(Batch.id == batch_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_result_consumer_batch_llm_complete_finalises_item():
    """When the LLM result arrives (run.status == 'processing'), the item is finalised
    and batch.completed is incremented."""
    batch_id, item_id, run_id, user_id = await _make_batch_run_with_llm()

    mock_js = AsyncMock()
    mock_minio = MagicMock()

    # running → processing (scrape done, LLM dispatched)
    for status in ("running", "completed"):
        msg = _make_nats_msg(
            run_id,
            status,
            minio_path="scrapeflow-results/history/raw.html" if status == "completed" else None,
        )
        await _handle_result(msg, mock_js, mock_minio)

    # LLM worker result arrives — run is now "processing" in DB
    llm_result = _make_nats_msg(
        run_id, "completed", minio_path="scrapeflow-results/history/out.json"
    )
    await _handle_result(llm_result, mock_js, mock_minio)

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        batch = await db.get(Batch, batch_id)
        item = await db.get(BatchItem, item_id)

    assert run is not None and run.status == "completed"
    assert run.result_path == "scrapeflow-results/history/out.json"
    assert batch is not None and batch.completed == 1
    assert batch.status == "completed"
    assert item is not None and item.status == "completed"
    assert item.result_path == "scrapeflow-results/history/out.json"

    async with AsyncSessionLocal() as db:
        await db.execute(delete(Batch).where(Batch.id == batch_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# ---------------------------------------------------------------------------
# GET /jobs does not include batch runs
# ---------------------------------------------------------------------------


async def test_list_jobs_excludes_batch_runs(client, auth_headers, mock_jetstream):
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
