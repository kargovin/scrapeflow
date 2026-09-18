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
from app.models.storage_object import StorageObject
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
    """DELETE /jobs/{id}?permanent=true decrements storage_bytes_used by each ledger row."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.minio import get_minio
    from app.main import app

    async with AsyncSessionLocal() as db:
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        path = f"scrapeflow-results/history/{job.id}/scrape.html"
        run = JobRun(job_id=job.id, status="completed", result_path=path)
        db.add(run)
        await db.flush()
        db.add(StorageObject(user_id=quota_user.id, object_key=path, bytes=300, job_run_id=run.id))
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_used=500))
        await db.commit()
        job_id = job.id

    mock_minio = MagicMock()
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
        path = f"scrapeflow-results/history/{job.id}/scrape.html"
        run = JobRun(job_id=job.id, status="completed", result_path=path)
        db.add(run)
        await db.flush()
        db.add(StorageObject(user_id=quota_user.id, object_key=path, bytes=400, job_run_id=run.id))
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_used=800))
        await db.commit()
        job_id = job.id

    mock_minio = MagicMock()
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
    """If the ledger write raises, the run is marked failed (not completed)."""
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

    with patch("app.core.result_consumer.record_object", side_effect=Exception("db error")):
        await _handle_result(msg, AsyncMock(), mock_minio)

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
        assert updated_run is not None
        assert updated_run.status == "failed"
        assert updated_run.error == "storage_accounting_failed"

    msg.ack.assert_called_once()


async def test_record_object_idempotent_on_key(db_user):
    """record_object charges a key once: the second call is a no-op and reports it.

    This is the redelivery guard the per-run stamp used to provide — keyed on the
    object now, so a second *artifact* for the same run is still charged (BUG-007).
    """
    from app.core.ledger import record_object

    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        key = f"scrapeflow-results/history/{run.id}/scrape.html"
        first = await record_object(
            db, user_id=db_user.id, object_key=key, size=1024, job_run_id=run.id
        )
        second = await record_object(
            db, user_id=db_user.id, object_key=key, size=1024, job_run_id=run.id
        )
        other = await record_object(
            db,
            user_id=db_user.id,
            object_key=f"scrapeflow-results/history/{run.id}/llm.json",
            size=16,
            job_run_id=run.id,
        )
        await db.commit()

        assert (first, second, other) == (True, False, True)
        quota_row = await db.get(UserQuota, db_user.id)
        assert quota_row.storage_bytes_used == 1024 + 16


# ---------------------------------------------------------------------------
# P7 — the crawl lane is metered, and the count meters read the views
# ---------------------------------------------------------------------------
#
# `quota_user`'s teardown deletes the user row, and crawls/batches reference it
# without ON DELETE CASCADE — so every test here that creates one removes it.

from app.models.batch import Batch, BatchItem  # noqa: E402
from app.models.crawl import Crawl, CrawlPage  # noqa: E402


async def _make_crawl(user_id, *, status="running", pages=0, page_status="pending"):
    """A crawl row plus `pages` crawl_pages rows, the shape the coordinator writes."""
    async with AsyncSessionLocal() as db:
        crawl = Crawl(user_id=user_id, seed_url="https://example.com", status=status)
        db.add(crawl)
        await db.flush()
        for i in range(pages):
            db.add(
                CrawlPage(
                    crawl_id=crawl.id, url=f"https://example.com/{i}", depth=0, status=page_status
                )
            )
        await db.commit()
        return crawl.id


async def _drop_crawls(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Crawl).where(Crawl.user_id == user_id))
        await db.commit()


async def _drop_batches(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Batch).where(Batch.user_id == user_id))
        await db.commit()


async def test_crawl_admission_prechecks_max_pages_against_monthly_runs(
    quota_client, quota_user, quota_headers
):
    """POST /crawls is pre-checked with batch_count=max_pages, as a batch is with len(urls)."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=10))
        await db.commit()

    resp = await quota_client.post(
        "/crawls", json={"seed_url": "https://example.com", "max_pages": 11}, headers=quota_headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "monthly_runs"

    resp = await quota_client.post(
        "/crawls", json={"seed_url": "https://example.com", "max_pages": 10}, headers=quota_headers
    )
    assert resp.status_code == 201, resp.text
    await _drop_crawls(quota_user.id)


async def test_crawl_pages_count_toward_monthly_runs(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """A crawl's pages are attempted fetches: 5 pages this month fill a limit of 5, and
    the *job* lane is what gets refused — the meter is one number across lanes."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=5))
        await db.commit()
    await _make_crawl(quota_user.id, status="completed", pages=5, page_status="completed")

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["quota_type"] == "monthly_runs"
    assert "5/5" in detail["message"]
    await _drop_crawls(quota_user.id)


async def test_active_crawl_holds_one_concurrent_slot(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """A queued crawl with no pages yet still holds its slot — the slot belongs to the
    submission, not to rows the coordinator has not written yet."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=1))
        await db.commit()
    await _make_crawl(quota_user.id, status="queued", pages=0)

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "concurrent_jobs"
    await _drop_crawls(quota_user.id)


async def test_crawl_with_many_active_pages_is_still_one_slot(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """Contention is per submission: 50 pending pages under one crawl occupy one slot."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=2))
        await db.commit()
    await _make_crawl(quota_user.id, status="running", pages=50, page_status="pending")

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 201, resp.text
    await _drop_crawls(quota_user.id)


async def test_terminal_crawl_holds_no_slot(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=1))
        await db.commit()
    await _make_crawl(quota_user.id, status="cancelled", pages=3, page_status="pending")

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 201, resp.text
    await _drop_crawls(quota_user.id)


async def test_crawl_admission_refused_when_pool_is_full(quota_client, quota_user, quota_headers):
    """The pool is shared: a pending job run blocks a new crawl."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=1))
        job = Job(user_id=quota_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        db.add(JobRun(job_id=job.id, status="pending"))
        await db.commit()

    resp = await quota_client.post(
        "/crawls", json={"seed_url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "concurrent_jobs"


async def test_crawl_admission_refused_at_storage_wall(quota_client, quota_user, quota_headers):
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, storage_bytes_limit=1000, storage_bytes_used=1000))
        await db.commit()

    resp = await quota_client.post(
        "/crawls", json={"seed_url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "storage_bytes"


async def test_batch_holds_one_concurrent_slot(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """ADR-009 §8, owner's call 2026-08-17: a batch of any size is one submission.
    Before P7 this batch metered as 5 against a limit of 2 and the job was refused."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, concurrent_jobs_limit=2))
        batch = Batch(user_id=quota_user.id, total=5, status="running")
        db.add(batch)
        await db.flush()
        for i in range(5):
            item = BatchItem(batch_id=batch.id, url=f"https://example.com/{i}", status="pending")
            db.add(item)
            await db.flush()
            db.add(JobRun(batch_item_id=item.id, status="pending"))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 201, resp.text
    await _drop_batches(quota_user.id)


async def test_batch_items_each_count_toward_monthly_runs(
    quota_client, quota_user, quota_headers, mock_jetstream
):
    """Cost is per fetch even though contention is per submission: the same 5-run batch
    is 5 monthly units."""
    async with AsyncSessionLocal() as db:
        db.add(UserQuota(user_id=quota_user.id, monthly_runs_limit=5))
        batch = Batch(user_id=quota_user.id, total=5, status="completed")
        db.add(batch)
        await db.flush()
        for i in range(5):
            item = BatchItem(batch_id=batch.id, url=f"https://example.com/{i}", status="completed")
            db.add(item)
            await db.flush()
            db.add(JobRun(batch_item_id=item.id, status="completed"))
        await db.commit()

    resp = await quota_client.post(
        "/jobs", json={"url": "https://example.com"}, headers=quota_headers
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["quota_type"] == "monthly_runs"
    await _drop_batches(quota_user.id)
