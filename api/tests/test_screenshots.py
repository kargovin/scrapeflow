"""BUG-004: a run's screenshots are listed on its result and served as PNGs, from the ledger."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from miniopy_async.error import S3Error
from sqlalchemy import delete

from app.auth.api_key import generate_api_key, hash_api_key
from app.core.db import AsyncSessionLocal
from app.core.minio import get_minio
from app.main import app
from app.models.api_key import ApiKey
from app.models.job import Job, OutputFormat
from app.models.job_runs import JobRun
from app.models.storage_object import StorageObject
from app.models.user import User

PNG = b"\x89PNG\r\n\x1a\nfake"
BUCKET = "scrapeflow-results"


def _headers(raw: str) -> dict:
    return {"X-API-Key": raw}


async def _user(is_admin: bool = False) -> tuple[User, str]:
    raw = generate_api_key()
    user = User(
        clerk_id=f"user_{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex}@example.com",
        is_admin=is_admin,
    )
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.flush()
        db.add(ApiKey(user_id=user.id, key_hash=hash_api_key(raw), name="k"))
        await db.commit()
        await db.refresh(user)
    return user, raw


@pytest_asyncio.fixture
async def users():
    owner, owner_key = await _user()
    other, other_key = await _user()
    admin, admin_key = await _user(is_admin=True)
    yield {"owner": (owner, owner_key), "other": other_key, "admin": admin_key}
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id.in_([owner.id, other.id, admin.id])))
        await db.commit()


async def _job_run(user: User, shots: int) -> tuple[Job, JobRun]:
    """A completed run holding its page and ``shots`` screenshots, stored out of order."""
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user.id, url="https://shots.example.com", output_format=OutputFormat.html)
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed", completed_at=datetime.now(UTC))
        db.add(run)
        await db.flush()
        run.result_path = f"{BUCKET}/history/{run.id}/scrape.html"
        db.add(
            StorageObject(user_id=user.id, object_key=run.result_path, bytes=10, job_run_id=run.id)
        )
        for index in reversed(range(shots)):
            db.add(
                StorageObject(
                    user_id=user.id,
                    object_key=f"{BUCKET}/screenshots/{run.id}/{index}.png",
                    bytes=1000 + index,
                    job_run_id=run.id,
                )
            )
        await db.commit()
        return job, run


def _minio(body: bytes = PNG) -> MagicMock:
    response = MagicMock()
    response.read = AsyncMock(return_value=body)
    response.release = AsyncMock()
    minio = MagicMock()
    minio.get_object = AsyncMock(return_value=response)
    return minio


@pytest.fixture
def minio():
    mock = _minio()
    app.dependency_overrides[get_minio] = lambda: mock
    yield mock
    app.dependency_overrides.pop(get_minio, None)


async def test_the_result_lists_the_runs_screenshots_in_action_order(client, users, minio):
    owner, key = users["owner"]
    job, run = await _job_run(owner, shots=3)

    for url in (f"/jobs/{job.id}/result", f"/jobs/{job.id}/runs/{run.id}/result"):
        resp = await client.get(url, headers=_headers(key))
        assert resp.status_code == 200
        assert resp.json()["screenshots"] == [
            {"index": 0, "bytes": 1000},
            {"index": 1, "bytes": 1001},
            {"index": 2, "bytes": 1002},
        ]

    resp = await client.get(f"/admin/jobs/{job.id}/result", headers=_headers(users["admin"]))
    assert [s["index"] for s in resp.json()["screenshots"]] == [0, 1, 2]


async def test_a_run_without_screenshots_lists_none(client, users, minio):
    owner, key = users["owner"]
    job, _ = await _job_run(owner, shots=0)

    resp = await client.get(f"/jobs/{job.id}/result", headers=_headers(key))

    assert resp.json()["screenshots"] == []


async def test_the_owner_gets_the_png(client, users, minio):
    owner, key = users["owner"]
    job, run = await _job_run(owner, shots=2)

    resp = await client.get(f"/jobs/{job.id}/runs/{run.id}/screenshots/1", headers=_headers(key))

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == PNG
    minio.get_object.assert_awaited_once_with(BUCKET, f"screenshots/{run.id}/1.png")


async def test_an_admin_gets_any_users_png(client, users, minio):
    owner, _ = users["owner"]
    job, run = await _job_run(owner, shots=1)

    resp = await client.get(
        f"/admin/jobs/{job.id}/runs/{run.id}/screenshots/0", headers=_headers(users["admin"])
    )

    assert resp.status_code == 200
    assert resp.content == PNG


async def test_another_user_gets_404(client, users, minio):
    owner, _ = users["owner"]
    job, run = await _job_run(owner, shots=1)

    resp = await client.get(
        f"/jobs/{job.id}/runs/{run.id}/screenshots/0", headers=_headers(users["other"])
    )

    assert resp.status_code == 404
    minio.get_object.assert_not_called()


async def test_a_run_of_another_job_gets_404(client, users, minio):
    owner, key = users["owner"]
    job, _ = await _job_run(owner, shots=1)
    _, foreign_run = await _job_run(owner, shots=1)

    resp = await client.get(
        f"/jobs/{job.id}/runs/{foreign_run.id}/screenshots/0", headers=_headers(key)
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run not found"


async def test_an_index_without_a_ledger_row_gets_404(client, users, minio):
    owner, key = users["owner"]
    job, run = await _job_run(owner, shots=1)

    resp = await client.get(f"/jobs/{job.id}/runs/{run.id}/screenshots/1", headers=_headers(key))

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Screenshot not found"
    minio.get_object.assert_not_called()


async def test_a_ledger_row_whose_object_is_gone_gets_404(client, users, minio):
    owner, key = users["owner"]
    job, run = await _job_run(owner, shots=1)
    minio.get_object.side_effect = S3Error(
        code="NoSuchKey",
        message="m",
        resource="/x",
        request_id="r",
        host_id="h",
        response=MagicMock(),
    )

    resp = await client.get(f"/jobs/{job.id}/runs/{run.id}/screenshots/0", headers=_headers(key))

    assert resp.status_code == 404
    assert resp.json()["detail"] == "The stored screenshot is missing"
