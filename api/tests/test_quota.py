"""Tests for Step 25 — PRD-012: billing/quotas enforcement + admin endpoint.

Uses isolated DB users (via db_user + ApiKey, not mock Clerk) so each test
starts with zero existing job_runs and no quota row — no cross-test interference.

Tests that expect 429 use `quota_client` (real quota checks active).
Tests that only check admin endpoint behaviour use `client` (quota checks disabled).
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.auth.api_key import generate_api_key, hash_api_key
from app.core.db import AsyncSessionLocal
from app.core.result_consumer import _handle_result
from app.models.api_key import ApiKey
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user import User
from app.models.user_quota import UserQuota

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def quota_user():
    """Fresh user with no quota row and no existing runs."""
    user = User(clerk_id=f"quota_{uuid.uuid4().hex}", email=f"quota_{uuid.uuid4().hex}@test.com")
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
        await db.refresh(user)
    yield user
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest_asyncio.fixture
async def quota_api_key(quota_user):
    """ApiKey for quota_user. Cleanup is cascade from quota_user."""
    raw_key = generate_api_key()
    key = ApiKey(user_id=quota_user.id, key_hash=hash_api_key(raw_key), name="quota test key")
    async with AsyncSessionLocal() as db:
        db.add(key)
        await db.commit()
        await db.refresh(key)
    return raw_key, key


@pytest.fixture
def quota_headers(quota_api_key):
    raw_key, _ = quota_api_key
    return {"X-API-Key": raw_key}


@pytest_asyncio.fixture
async def admin_user():
    user = User(
        clerk_id=f"admin_{uuid.uuid4().hex}",
        email=f"admin_{uuid.uuid4().hex}@test.com",
        is_admin=True,
    )
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
        await db.refresh(user)
    yield user
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest_asyncio.fixture
async def admin_api_key(admin_user):
    raw_key = generate_api_key()
    key = ApiKey(user_id=admin_user.id, key_hash=hash_api_key(raw_key), name="admin quota key")
    async with AsyncSessionLocal() as db:
        db.add(key)
        await db.commit()
    return raw_key, key


@pytest.fixture
def admin_headers(admin_api_key):
    raw_key, _ = admin_api_key
    return {"X-API-Key": raw_key}


# ---------------------------------------------------------------------------
# POST /jobs — monthly_runs quota  (quota_client = real enforcement)
# ---------------------------------------------------------------------------


async def test_monthly_runs_quota_allows_exactly_at_limit(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """POST /jobs succeeds when the run count is exactly at the limit."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=1))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 201


async def test_monthly_runs_quota_exceeded(quota_client, quota_user, quota_headers, mock_jetstream):
    """POST /jobs returns 429 when monthly run limit is already reached."""
    async with AsyncSessionLocal() as db:
        # Limit = 1, and create one run this month to fill it.
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=1))
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="completed"))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["quota_type"] == "monthly_runs"
    assert "resets_at" in detail


async def test_monthly_runs_quota_correct_format(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """429 response includes all required fields from the spec."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="completed"))
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=1))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["quota_type"] == "monthly_runs"
    assert "message" in detail
    assert "resets_at" in detail


# ---------------------------------------------------------------------------
# POST /jobs — concurrent_jobs quota
# ---------------------------------------------------------------------------


async def test_concurrent_jobs_quota_exceeded(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """POST /jobs returns 429 when concurrent limit is reached."""
    async with AsyncSessionLocal() as db:
        # Limit = 1, and create one pending run to fill it.
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=1))
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="pending"))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["quota_type"] == "concurrent_jobs"
    assert detail["resets_at"] is None


async def test_concurrent_jobs_completed_runs_not_counted(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """Completed runs do not count toward the concurrent limit."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=1))
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="completed"))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    # Completed run doesn't block a new job.
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# POST /batch — monthly_runs quota (batch_count check)
# ---------------------------------------------------------------------------


async def test_batch_monthly_runs_quota_exceeded(quota_client, quota_user, quota_headers):
    """POST /batch returns 429 when the batch would push the user over monthly_runs."""
    async with AsyncSessionLocal() as db:
        # Limit = 2. One run already used this month.
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="completed"))
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=2))
        await db.commit()

    # Batch of 2 would push total to 3 > limit=2.
    resp = await quota_client.post(
        "/batch",
        json={"urls": ["https://example.com/a", "https://example.com/b"]},
        headers=quota_headers,
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "monthly_runs"


# ---------------------------------------------------------------------------
# PATCH /admin/users/{id}/quota  (client = quota checks disabled, irrelevant here)
# ---------------------------------------------------------------------------


async def test_admin_patch_quota_creates_row(client, quota_user, admin_headers):
    """PATCH /admin/users/{id}/quota creates a user_quotas row when none exists."""
    resp = await client.patch(
        f"/admin/users/{quota_user.id}/quota",
        json={"monthly_runs_limit": 100},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["monthly_runs_limit"] == 100
    assert data["concurrent_jobs_limit"] is None
    assert data["storage_bytes_limit"] is None
    assert data["storage_bytes_used"] == 0


async def test_admin_patch_quota_updates_existing_row(client, quota_user, admin_headers):
    """PATCH only updates provided fields; other limits are preserved."""
    async with AsyncSessionLocal() as db:
        db.add(
            UserQuota(
                user_id=quota_user.id,
                monthly_runs_limit=50,
                concurrent_jobs_limit=3,
            )
        )
        await db.commit()

    resp = await client.patch(
        f"/admin/users/{quota_user.id}/quota",
        json={"monthly_runs_limit": 200},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["monthly_runs_limit"] == 200
    assert data["concurrent_jobs_limit"] == 3  # unchanged


async def test_admin_patch_quota_enforced_on_next_job(
    quota_client, quota_user, quota_headers, admin_headers, mock_jetstream
):
    """Custom limit set via admin endpoint is enforced on the next job creation."""
    # Set limit to 1 via admin.
    resp = await quota_client.patch(
        f"/admin/users/{quota_user.id}/quota",
        json={"monthly_runs_limit": 1},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Use that one slot.
    resp1 = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp1.status_code == 201

    # Second job should be blocked.
    resp2 = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp2.status_code == 429
    assert resp2.json()["detail"]["quota_type"] == "monthly_runs"


async def test_admin_patch_quota_404_for_missing_user(client, admin_headers):
    """PATCH /admin/users/{id}/quota returns 404 for a non-existent user."""
    resp = await client.patch(
        f"/admin/users/{uuid.uuid4()}/quota",
        json={"monthly_runs_limit": 100},
        headers=admin_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Result consumer — storage quota enforcement
# ---------------------------------------------------------------------------


async def test_result_consumer_storage_quota_exceeded_fails_run(quota_user):
    """Result consumer fails a run and removes the MinIO object when over storage quota."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        # Storage limit = 100 bytes, already used 90 — a 50-byte object would exceed it.
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_limit=100, storage_bytes_used=90))
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    minio_path = f"scrapeflow-results/history/{job_id}/1234567890.html"

    # stat_object returns 50 bytes — would push used to 140 > 100.
    stat_obj = MagicMock()
    stat_obj.size = 50
    mock_minio = AsyncMock()
    mock_minio.stat_object = AsyncMock(return_value=stat_obj)
    mock_minio.remove_object = AsyncMock()

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

    await _handle_result(msg, AsyncMock(), mock_minio)

    # MinIO object must be deleted.
    mock_minio.remove_object.assert_called_once()

    # Run must be failed with storage_quota_exceeded.
    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "failed"
        assert updated_run.error == "storage_quota_exceeded"

    msg.ack.assert_called_once()


async def test_result_consumer_storage_quota_ok_increments_usage(quota_user):
    """Result consumer increments storage_bytes_used on a successful completion."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_limit=1000, storage_bytes_used=0))
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    minio_path = f"scrapeflow-results/history/{job_id}/1234567890.html"

    stat_obj = MagicMock()
    stat_obj.size = 200
    mock_minio = AsyncMock()
    mock_minio.stat_object = AsyncMock(return_value=stat_obj)

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

    await _handle_result(msg, AsyncMock(), mock_minio)

    async with AsyncSessionLocal() as db:
        quota_row = await db.get(UserQuota, quota_user.id)
        assert quota_row is not None
        assert quota_row.storage_bytes_used == 200

    msg.ack.assert_called_once()


# ---------------------------------------------------------------------------
# DELETE /jobs/{id}?permanent=true — storage quota decrement
# ---------------------------------------------------------------------------


async def test_permanent_delete_decrements_storage_quota(
    client, quota_user, quota_headers, mock_jetstream
):
    """DELETE /jobs/{id}?permanent=true decrements storage_bytes_used for each deleted run."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.minio import get_minio
    from app.main import app

    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(
            job_id=job.id,
            status="completed",
            result_path=f"scrapeflow-results/history/{job.id}/123.html",
        )
        db.add(run)
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_used=500))
        await db.commit()
        job_id = job.id

    stat_obj = MagicMock()
    stat_obj.size = 300
    mock_minio = MagicMock()
    mock_minio.stat_object = AsyncMock(return_value=stat_obj)
    mock_minio.remove_object = AsyncMock()

    app.dependency_overrides[get_minio] = lambda: mock_minio
    try:
        resp = await client.delete(f"/jobs/{job_id}?permanent=true", headers=quota_headers)
        assert resp.status_code == 204
    finally:
        app.dependency_overrides.pop(get_minio, None)

    async with AsyncSessionLocal() as db:
        quota_row = await db.get(UserQuota, quota_user.id)
        assert quota_row is not None
        assert quota_row.storage_bytes_used == 200  # 500 - 300


# ---------------------------------------------------------------------------
# DELETE /admin/jobs/{id}?hard_delete=true — storage quota decrement
# ---------------------------------------------------------------------------


async def test_admin_hard_delete_decrements_storage_quota(client, quota_user, admin_headers):
    """DELETE /admin/jobs/{id}?hard_delete=true decrements storage_bytes_used and cleans MinIO."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.minio import get_minio
    from app.main import app

    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(
            job_id=job.id,
            status="completed",
            result_path=f"scrapeflow-results/history/{job.id}/456.html",
        )
        db.add(run)
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_used=800))
        await db.commit()
        job_id = job.id

    stat_obj = MagicMock()
    stat_obj.size = 400
    mock_minio = MagicMock()
    mock_minio.stat_object = AsyncMock(return_value=stat_obj)
    mock_minio.remove_object = AsyncMock()

    app.dependency_overrides[get_minio] = lambda: mock_minio
    try:
        resp = await client.delete(f"/admin/jobs/{job_id}?hard_delete=true", headers=admin_headers)
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(get_minio, None)

    mock_minio.remove_object.assert_called_once()

    async with AsyncSessionLocal() as db:
        quota_row = await db.get(UserQuota, quota_user.id)
        assert quota_row is not None
        assert quota_row.storage_bytes_used == 400  # 800 - 400


# ---------------------------------------------------------------------------
# Result consumer — storage increment failure (step 9)
# ---------------------------------------------------------------------------


async def test_result_consumer_storage_increment_failure_fails_run(quota_user):
    """If increment_storage_bytes raises, the run is marked failed (not completed)."""
    from unittest.mock import patch

    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_limit=10_000, storage_bytes_used=0))
        await db.commit()
        await db.refresh(job)
        await db.refresh(run)
        job_id = job.id
        run_id = run.id

    minio_path = f"scrapeflow-results/history/{job_id}/1234567890.html"

    stat_obj = MagicMock()
    stat_obj.size = 100
    mock_minio = AsyncMock()
    mock_minio.stat_object = AsyncMock(return_value=stat_obj)
    mock_minio.get_object = AsyncMock(side_effect=Exception("minio error"))

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

    with patch(
        "app.core.result_consumer.increment_storage_bytes", side_effect=Exception("db error")
    ):
        await _handle_result(msg, AsyncMock(), mock_minio)

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "failed"
        assert updated_run.error == "storage_accounting_failed"

    msg.ack.assert_called_once()


async def test_try_increment_storage_idempotent(db_user):
    """Finding 5: _try_increment_storage is a no-op when run.storage_accounted_at is already set.

    Belt-and-suspenders guard: even if the terminal-status early-return is somehow
    bypassed, a run that already has storage_accounted_at set must not increment the
    quota a second time.
    """
    from unittest.mock import patch

    from app.core.result_consumer import _try_increment_storage

    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        from datetime import UTC, datetime

        run = JobRun(
            job_id=job.id,
            status="completed",
            storage_accounted_at=datetime.now(UTC),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        with patch("app.core.result_consumer.increment_storage_bytes") as mock_inc:
            result = await _try_increment_storage(db, run, db_user.id, size=1024)

        assert result is True, "should return True (accounting already recorded)"
        mock_inc.assert_not_called()
