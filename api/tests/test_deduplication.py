import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import xxhash
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.core.result_consumer import _handle_result
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.user import User


async def _make_job_and_completed_run(
    content_hash: str | None = None,
    result_path: str | None = None,
):
    """Create user + job + one completed run. Returns (user_id, job_id, run_id)."""
    async with AsyncSessionLocal() as db:
        user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@test.com")
        db.add(user)
        await db.flush()

        job = Job(user_id=user.id, url="https://example.com")
        db.add(job)
        await db.flush()

        run = JobRun(
            job_id=job.id,
            status="completed",
            content_hash=content_hash,
            result_path=result_path,
            completed_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db.add(run)
        await db.commit()
        await db.refresh(user)
        await db.refresh(job)
        await db.refresh(run)

    return user.id, job.id, run.id


async def _add_pending_run(job_id: uuid.UUID) -> uuid.UUID:
    """Append a fresh pending run to an existing job. Returns run_id."""
    async with AsyncSessionLocal() as db:
        run = JobRun(job_id=job_id, status="pending")
        db.add(run)
        await db.commit()
        await db.refresh(run)
    return run.id


def _nats_msg(job_id, run_id, status, minio_path=None, error=None):
    msg = MagicMock()
    msg.data = json.dumps(
        {
            "job_id": str(job_id),
            "run_id": str(run_id),
            "status": status,
            "minio_path": minio_path,
            "error": error,
        }
    ).encode()
    msg.metadata.timestamp = datetime.now(UTC)
    msg.ack = AsyncMock()
    return msg


def _mock_minio(content: bytes) -> AsyncMock:
    """Mock Minio where get_object returns `content` and stat_object raises (result_size=0)."""
    mock = AsyncMock()
    mock.stat_object.side_effect = Exception("no stat in test")

    def _resp():
        r = AsyncMock()
        r.read = AsyncMock(return_value=content)
        r.close = MagicMock()
        return r

    mock.get_object = AsyncMock(side_effect=lambda *_a, **_kw: _resp())
    return mock


async def test_result_consumer_dedup_same_content_short_circuits():
    """Second run with identical content: result_path redirected to prev, history/ deleted."""
    content = b"<html>same page</html>"
    content_hash = xxhash.xxh64(content).hexdigest()[:16]
    prev_path = "scrapeflow-results/history/prev_run.md"

    user_id, job_id, _ = await _make_job_and_completed_run(
        content_hash=content_hash, result_path=prev_path
    )
    new_run_id = await _add_pending_run(job_id)

    mock_minio = _mock_minio(content)
    mock_js = AsyncMock()

    await _handle_result(_nats_msg(job_id, new_run_id, "running"), mock_js, mock_minio)
    await _handle_result(
        _nats_msg(job_id, new_run_id, "completed", minio_path="scrapeflow-results/history/run.md"),
        mock_js,
        mock_minio,
    )

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, new_run_id)

    assert run.status == "completed"
    assert run.diff_detected is False
    assert run.content_hash == content_hash
    assert run.result_path == prev_path
    mock_minio.remove_object.assert_called_once_with("scrapeflow-results", "history/run.md")
    mock_js.publish.assert_not_called()

    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_result_consumer_dedup_prev_has_no_result_path_proceeds_normally():
    """Dedup match where prev run has no result_path: skip dedup, keep new object as canonical."""
    content = b"<html>same page</html>"
    content_hash = xxhash.xxh64(content).hexdigest()[:16]

    # Previous run has matching hash but no result_path (e.g. created before dedup was introduced)
    user_id, job_id, _ = await _make_job_and_completed_run(
        content_hash=content_hash, result_path=None
    )
    new_run_id = await _add_pending_run(job_id)

    mock_minio = _mock_minio(content)
    mock_js = AsyncMock()

    await _handle_result(_nats_msg(job_id, new_run_id, "running"), mock_js, mock_minio)
    await _handle_result(
        _nats_msg(job_id, new_run_id, "completed", minio_path="scrapeflow-results/history/run.md"),
        mock_js,
        mock_minio,
    )

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, new_run_id)

    assert run.status == "completed"
    assert run.result_path == "scrapeflow-results/history/run.md"
    mock_minio.remove_object.assert_not_called()

    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_result_consumer_dedup_different_content_proceeds():
    """Different content hash: result_path stored, no history/ deletion, normal finalization."""
    old_content = b"<html>old page</html>"
    new_content = b"<html>new and different page</html>"
    old_hash = xxhash.xxh64(old_content).hexdigest()[:16]

    user_id, job_id, _ = await _make_job_and_completed_run(content_hash=old_hash)
    new_run_id = await _add_pending_run(job_id)

    mock_minio = _mock_minio(new_content)
    mock_js = AsyncMock()

    await _handle_result(_nats_msg(job_id, new_run_id, "running"), mock_js, mock_minio)
    await _handle_result(
        _nats_msg(job_id, new_run_id, "completed", minio_path="scrapeflow-results/history/run.md"),
        mock_js,
        mock_minio,
    )

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, new_run_id)

    new_hash = xxhash.xxh64(new_content).hexdigest()[:16]
    assert run.status == "completed"
    assert run.content_hash == new_hash
    assert run.result_path == "scrapeflow-results/history/run.md"
    mock_minio.remove_object.assert_not_called()

    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_result_consumer_dedup_no_previous_run_proceeds():
    """First run ever (no previous completed run): proceeds normally without dedup."""
    content = b"<html>first ever run</html>"

    async with AsyncSessionLocal() as db:
        user = User(clerk_id=f"user_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@test.com")
        db.add(user)
        await db.flush()
        job = Job(user_id=user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="pending")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        user_id = user.id
        job_id = job.id
        run_id = run.id

    mock_minio = _mock_minio(content)
    mock_js = AsyncMock()

    await _handle_result(_nats_msg(job_id, run_id, "running"), mock_js, mock_minio)
    await _handle_result(
        _nats_msg(job_id, run_id, "completed", minio_path="scrapeflow-results/history/first.md"),
        mock_js,
        mock_minio,
    )

    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)

    expected_hash = xxhash.xxh64(content).hexdigest()[:16]
    assert run.status == "completed"
    assert run.content_hash == expected_hash
    assert run.result_path == "scrapeflow-results/history/first.md"
    mock_minio.remove_object.assert_not_called()

    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
