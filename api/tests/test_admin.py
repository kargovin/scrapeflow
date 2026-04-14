"""Tests for Step 23 — Admin Panel API routes.

All routes require is_admin=True; non-admin users get 403.
Admin bypasses ownership checks — can read/modify any user's resources.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.auth.api_key import generate_api_key, hash_api_key
from app.core.db import AsyncSessionLocal
from app.models.api_key import ApiKey
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery

# ---------------------------------------------------------------------------
# Local fixtures — not in conftest because they are admin-specific
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_user():
    """A User with is_admin=True. Cleaned up after each test."""
    user = User(
        clerk_id=f"admin_{uuid.uuid4().hex}",
        email=f"admin_{uuid.uuid4().hex}@example.com",
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
    """ApiKey for admin_user. Cleanup is handled by admin_user's cascade delete."""
    raw_key = generate_api_key()
    key = ApiKey(
        user_id=admin_user.id,
        key_hash=hash_api_key(raw_key),
        name="admin fixture key",
    )
    async with AsyncSessionLocal() as db:
        db.add(key)
        await db.commit()
        await db.refresh(key)
    return raw_key, key


@pytest.fixture
def admin_headers(admin_api_key):
    raw_key, _ = admin_api_key
    return {"X-API-Key": raw_key}


# ---------------------------------------------------------------------------
# 403 for non-admin on all 8 routes
# ---------------------------------------------------------------------------


async def test_admin_routes_403_for_non_admin(client, db_api_key):
    """Every admin route returns 403 when the caller is a non-admin user."""
    raw_key, _ = db_api_key
    headers = {"X-API-Key": raw_key}
    fake_id = uuid.uuid4()
    routes = [
        ("GET", "/admin/users"),
        ("GET", f"/admin/users/{fake_id}"),
        ("DELETE", f"/admin/users/{fake_id}"),
        ("GET", "/admin/jobs"),
        ("GET", f"/admin/jobs/{fake_id}"),
        ("DELETE", f"/admin/jobs/{fake_id}"),
        ("GET", "/admin/webhooks/deliveries"),
        ("POST", f"/admin/webhooks/deliveries/{fake_id}/retry"),
    ]
    for method, path in routes:
        resp = await client.request(method, path, headers=headers)
        assert resp.status_code == 403, f"Expected 403 for {method} {path}, got {resp.status_code}"


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


async def test_admin_list_users(client, admin_user, admin_headers):
    """Admin gets a 200 list that includes their own account with is_admin field."""
    resp = await client.get("/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    users = resp.json()
    # Session DB may accumulate users from other tests — assert presence, not exact count.
    assert all("is_admin" in u for u in users)
    assert any(u["id"] == str(admin_user.id) for u in users)


async def test_admin_list_users_email_filter(client, admin_user, admin_headers):
    """Email filter (partial, case-insensitive) narrows results to matching users."""
    resp = await client.get(
        "/admin/users", headers=admin_headers, params={"email": admin_user.email}
    )
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) == 1
    assert users[0]["id"] == str(admin_user.id)


# ---------------------------------------------------------------------------
# GET /admin/users/{id}
# ---------------------------------------------------------------------------


async def test_admin_get_user_detail(client, admin_headers, db_user):
    """User detail includes job_counts with at least one status key."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://detail.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.commit()

    resp = await client.get(f"/admin/users/{db_user.id}", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(db_user.id)
    assert "job_counts" in data
    assert data["job_counts"].get("completed", 0) >= 1


async def test_admin_get_user_detail_404(client, admin_headers):
    resp = await client.get(f"/admin/users/{uuid.uuid4()}", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /admin/users/{id}
# ---------------------------------------------------------------------------


async def test_admin_delete_user(client, admin_headers, db_user):
    """Hard delete removes the user from the DB (fixture teardown is a no-op)."""
    user_id = db_user.id
    resp = await client.delete(f"/admin/users/{user_id}", headers=admin_headers)
    assert resp.status_code == 204

    async with AsyncSessionLocal() as db:
        gone = await db.get(User, user_id)
    assert gone is None


async def test_admin_delete_self_is_rejected(client, admin_user, admin_headers):
    """Deleting your own admin account returns 400."""
    resp = await client.delete(f"/admin/users/{admin_user.id}", headers=admin_headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /admin/jobs
# ---------------------------------------------------------------------------


async def test_admin_list_jobs_cross_tenant(client, admin_headers, db_user):
    """Admin sees jobs belonging to a different (non-admin) user."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://cross-tenant.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.commit()
        job_id = job.id

    resp = await client.get("/admin/jobs", headers=admin_headers)
    assert resp.status_code == 200
    ids = [j["id"] for j in resp.json()]
    assert str(job_id) in ids


async def test_admin_list_jobs_filter_user_id(client, admin_headers, db_user):
    """user_id filter restricts results to that user's jobs only."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://filter-uid.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending")
        db.add(run)
        await db.commit()
        job_id = job.id

    resp = await client.get(
        "/admin/jobs", headers=admin_headers, params={"user_id": str(db_user.id)}
    )
    assert resp.status_code == 200
    jobs = resp.json()
    assert all(j["user_id"] == str(db_user.id) for j in jobs)
    assert any(j["id"] == str(job_id) for j in jobs)


async def test_admin_list_jobs_filter_status(client, admin_headers, db_user):
    """status filter matches on the latest run's status."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://filter-status.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="failed")
        db.add(run)
        await db.commit()
        job_id = job.id

    resp = await client.get(
        "/admin/jobs",
        headers=admin_headers,
        params={"user_id": str(db_user.id), "status": "failed"},
    )
    assert resp.status_code == 200
    jobs = resp.json()
    assert all(j["status"] == "failed" for j in jobs)
    assert any(j["id"] == str(job_id) for j in jobs)


async def test_admin_list_jobs_filter_engine(client, admin_headers, db_user):
    """engine filter excludes jobs with a different engine value."""
    async with AsyncSessionLocal() as db:
        pw_job = Job(user_id=db_user.id, url="https://pw.example.com", engine="playwright")
        http_job = Job(user_id=db_user.id, url="https://http.example.com", engine="http")
        db.add(pw_job)
        db.add(http_job)
        await db.flush()
        db.add(JobRun(job_id=pw_job.id, status="completed"))
        db.add(JobRun(job_id=http_job.id, status="completed"))
        await db.commit()
        pw_id = pw_job.id
        http_id = http_job.id

    resp = await client.get(
        "/admin/jobs",
        headers=admin_headers,
        params={"user_id": str(db_user.id), "engine": "playwright"},
    )
    assert resp.status_code == 200
    ids = [j["id"] for j in resp.json()]
    assert str(pw_id) in ids
    assert str(http_id) not in ids


# ---------------------------------------------------------------------------
# GET /admin/jobs/{id}
# ---------------------------------------------------------------------------


async def test_admin_get_job_cross_tenant(client, admin_headers, db_user):
    """Admin can retrieve a job owned by a different user — no 404."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://get-ct.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.commit()
        job_id = job.id

    resp = await client.get(f"/admin/jobs/{job_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == str(job_id)
    assert resp.json()["user_id"] == str(db_user.id)


async def test_admin_get_job_404(client, admin_headers):
    resp = await client.get(f"/admin/jobs/{uuid.uuid4()}", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /admin/jobs/{id}
# ---------------------------------------------------------------------------


async def test_admin_cancel_job(client, admin_headers, db_user):
    """Default delete (no hard_delete) cancels active runs in the DB."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://cancel.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending")
        db.add(run)
        await db.commit()
        job_id = job.id
        run_id = run.id

    resp = await client.delete(f"/admin/jobs/{job_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert "cancelled" in resp.json()["message"]

    async with AsyncSessionLocal() as db:
        updated_run = await db.get(JobRun, run_id)
    assert updated_run.status == "cancelled"


async def test_admin_hard_delete_job(client, admin_headers, db_user):
    """hard_delete=true removes the job row; CASCADE handles runs/deliveries."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://hard-del.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.commit()
        job_id = job.id

    resp = await client.delete(
        f"/admin/jobs/{job_id}", headers=admin_headers, params={"hard_delete": "true"}
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Job deleted"

    async with AsyncSessionLocal() as db:
        gone = await db.get(Job, job_id)
    assert gone is None


# ---------------------------------------------------------------------------
# GET /admin/webhooks/deliveries
# ---------------------------------------------------------------------------


async def test_admin_list_webhook_deliveries(client, admin_headers, db_user):
    """Admin sees all webhook deliveries; the seeded delivery appears in the list."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://wh-list.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.flush()
        delivery = WebhookDelivery(
            job_id=job.id,
            run_id=run.id,
            webhook_url="https://hooks.example.com/recv",
            payload={"event": "job.completed"},
            status="pending",
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.commit()
        delivery_id = delivery.id

    resp = await client.get("/admin/webhooks/deliveries", headers=admin_headers)
    assert resp.status_code == 200
    ids = [d["id"] for d in resp.json()]
    assert str(delivery_id) in ids


async def test_admin_list_webhook_deliveries_filter_status(client, admin_headers, db_user):
    """status filter returns only deliveries matching that status."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://wh-filter.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.flush()
        delivery = WebhookDelivery(
            job_id=job.id,
            run_id=run.id,
            webhook_url="https://hooks.example.com/recv",
            payload={"event": "job.completed"},
            status="exhausted",
            attempts=5,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.commit()
        delivery_id = delivery.id

    resp = await client.get(
        "/admin/webhooks/deliveries", headers=admin_headers, params={"status": "exhausted"}
    )
    assert resp.status_code == 200
    deliveries = resp.json()
    assert all(d["status"] == "exhausted" for d in deliveries)
    assert any(d["id"] == str(delivery_id) for d in deliveries)


# ---------------------------------------------------------------------------
# POST /admin/webhooks/deliveries/{id}/retry
# ---------------------------------------------------------------------------


async def test_admin_retry_webhook_delivery(client, admin_headers, db_user):
    """Retry resets attempts=0, status=pending, and updates next_attempt_at."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://wh-retry.example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed")
        db.add(run)
        await db.flush()
        delivery = WebhookDelivery(
            job_id=job.id,
            run_id=run.id,
            webhook_url="https://hooks.example.com/recv",
            payload={"event": "job.completed"},
            status="exhausted",
            attempts=5,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.commit()
        delivery_id = delivery.id

    resp = await client.post(
        f"/admin/webhooks/deliveries/{delivery_id}/retry", headers=admin_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["attempts"] == 0

    # Verify the DB was updated, not just the response
    async with AsyncSessionLocal() as db:
        d = await db.get(WebhookDelivery, delivery_id)
    assert d.status == "pending"
    assert d.attempts == 0
