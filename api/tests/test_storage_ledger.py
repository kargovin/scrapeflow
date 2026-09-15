"""The storage ledger (ADR-009 §8d, backlog P8) — and BUG-007, which it closes.

Every test here is about one question the old per-run stamp could not answer: which
objects does this user actually hold? The consumer records each stored object as a
row; the delete paths enumerate rows; the counter follows the rows.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.core.minio import get_minio
from app.core.result_consumer import _handle_result
from app.main import app
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.llm_keys import UserLLMKey
from app.models.storage_object import StorageObject
from app.models.user_quota import UserQuota

BUCKET = "scrapeflow-results"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(run_id, status, minio_path=None, source="scrape", screenshot_paths=None):
    msg = MagicMock()
    body = {"run_id": str(run_id), "status": status, "minio_path": minio_path, "source": source}
    if screenshot_paths is not None:
        body["screenshot_paths"] = screenshot_paths
    msg.data = json.dumps(body).encode()
    msg.metadata.timestamp = datetime.now(UTC)
    msg.ack = AsyncMock()
    return msg


def _minio_with_sizes(sizes: dict[str, int], content: bytes = b"<html>page</html>") -> AsyncMock:
    """stat_object answers from `sizes` by key; get_object serves `content` (for the hash)."""
    mock = AsyncMock()

    async def _stat(bucket, key):
        if key not in sizes:
            raise Exception(f"NoSuchKey {key}")
        stat = MagicMock()
        stat.size = sizes[key]
        return stat

    def _resp():
        r = AsyncMock()
        r.read = AsyncMock(return_value=content)
        r.close = MagicMock()
        return r

    mock.stat_object = AsyncMock(side_effect=_stat)
    mock.get_object = AsyncMock(side_effect=lambda *_a, **_kw: _resp())
    mock.remove_object = AsyncMock()
    return mock


async def _rows_for_run(run_id) -> list[StorageObject]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(StorageObject).where(StorageObject.job_run_id == run_id))
        return list(result.scalars().all())


async def _used(user_id) -> int:
    async with AsyncSessionLocal() as db:
        row = await db.get(UserQuota, user_id)
        return row.storage_bytes_used if row else 0


async def _make_llm_job(user_id):
    async with AsyncSessionLocal() as db:
        key = UserLLMKey(user_id=user_id, name="k", provider="anthropic", encrypted_api_key="enc")
        db.add(key)
        await db.flush()
        job = Job(
            user_id=user_id,
            url="https://example.com",
            llm_config={"llm_key_id": str(key.id), "model": "m", "output_schema": {}},
        )
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        db.add(UserQuota(user_id=user_id, storage_bytes_limit=10_000, storage_bytes_used=0))
        await db.commit()
        return job.id, run.id


# ---------------------------------------------------------------------------
# BUG-007 — an LLM job holds two objects and is charged for both
# ---------------------------------------------------------------------------


async def test_llm_job_charges_both_objects(db_user):
    """Scrape (2000 B) then LLM (50 B): two ledger rows, counter = 2050.

    Before P8 the second charge was skipped because the run was already stamped, so the
    user paid for the page and the extraction was free — and on delete the page was
    never found again.
    """
    _, run_id = await _make_llm_job(db_user.id)
    page = f"{BUCKET}/history/{run_id}/scrape.html"
    llm = f"{BUCKET}/history/{run_id}/llm.json"
    minio = _minio_with_sizes(
        {f"history/{run_id}/scrape.html": 2000, f"history/{run_id}/llm.json": 50}
    )

    await _handle_result(_msg(run_id, "completed", page), AsyncMock(), minio)
    async with AsyncSessionLocal() as db:
        assert (await db.get(JobRun, run_id)).status == "processing"
    assert await _used(db_user.id) == 2000

    await _handle_result(_msg(run_id, "completed", llm, source="llm"), AsyncMock(), minio)
    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        assert run.status == "completed"
        assert run.result_path == llm
        assert run.storage_accounted_at is None  # the stamp is not written any more

    rows = await _rows_for_run(run_id)
    assert {(r.object_key, r.bytes) for r in rows} == {(page, 2000), (llm, 50)}
    assert await _used(db_user.id) == 2050


async def test_permanent_delete_releases_every_ledger_row(client, db_user, mock_jetstream):
    """Delete an LLM job: both objects removed, both rows gone, counter back to zero.

    The delete path used to stat `result_path` — the JSON — and decrement by that,
    leaving the page on disk and the counter inflated by its size, forever.
    """
    from app.auth.api_key import generate_api_key, hash_api_key
    from app.models.api_key import ApiKey

    job_id, run_id = await _make_llm_job(db_user.id)
    page = f"{BUCKET}/history/{run_id}/scrape.html"
    llm = f"{BUCKET}/history/{run_id}/llm.json"
    minio = _minio_with_sizes(
        {f"history/{run_id}/scrape.html": 2000, f"history/{run_id}/llm.json": 50}
    )
    await _handle_result(_msg(run_id, "completed", page), AsyncMock(), minio)
    await _handle_result(_msg(run_id, "completed", llm, source="llm"), AsyncMock(), minio)
    assert await _used(db_user.id) == 2050

    raw = generate_api_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(user_id=db_user.id, key_hash=hash_api_key(raw), name="ledger"))
        await db.commit()

    app.dependency_overrides[get_minio] = lambda: minio
    try:
        resp = await client.delete(f"/jobs/{job_id}?permanent=true", headers={"X-API-Key": raw})
        assert resp.status_code == 204
    finally:
        app.dependency_overrides.pop(get_minio, None)

    removed = {call.args[1] for call in minio.remove_object.call_args_list}
    assert removed == {f"history/{run_id}/scrape.html", f"history/{run_id}/llm.json"}
    assert await _rows_for_run(run_id) == []
    assert await _used(db_user.id) == 0


# ---------------------------------------------------------------------------
# Screenshots (BUG-004, facet 2) — stored objects like any other
# ---------------------------------------------------------------------------


async def _make_plain_job(user_id, limit=10_000, used=0):
    async with AsyncSessionLocal() as db:
        job = Job(user_id=user_id, url="https://example.com")
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="running")
        db.add(run)
        db.add(UserQuota(user_id=user_id, storage_bytes_limit=limit, storage_bytes_used=used))
        await db.commit()
        return job.id, run.id


async def test_screenshots_are_recorded_and_charged(db_user):
    _, run_id = await _make_plain_job(db_user.id)
    page = f"{BUCKET}/history/{run_id}/scrape.html"
    shots = [f"{BUCKET}/screenshots/{run_id}/0.png", f"{BUCKET}/screenshots/{run_id}/1.png"]
    minio = _minio_with_sizes(
        {
            f"history/{run_id}/scrape.html": 100,
            f"screenshots/{run_id}/0.png": 30,
            f"screenshots/{run_id}/1.png": 40,
        }
    )

    await _handle_result(
        _msg(run_id, "completed", page, screenshot_paths=shots), AsyncMock(), minio
    )

    rows = await _rows_for_run(run_id)
    assert {(r.object_key, r.bytes) for r in rows} == {(page, 100), (shots[0], 30), (shots[1], 40)}
    assert await _used(db_user.id) == 170


async def test_screenshots_count_toward_the_quota_check(db_user):
    """Page 50 B + screenshot 60 B against a 100 B limit: over. Everything is deleted,
    nothing is recorded, the run fails — the same posture as an oversized page alone."""
    _, run_id = await _make_plain_job(db_user.id, limit=100)
    page = f"{BUCKET}/history/{run_id}/scrape.html"
    shot = f"{BUCKET}/screenshots/{run_id}/0.png"
    minio = _minio_with_sizes(
        {f"history/{run_id}/scrape.html": 50, f"screenshots/{run_id}/0.png": 60}
    )

    await _handle_result(
        _msg(run_id, "completed", page, screenshot_paths=[shot]), AsyncMock(), minio
    )

    removed = {call.args[1] for call in minio.remove_object.call_args_list}
    assert removed == {f"history/{run_id}/scrape.html", f"screenshots/{run_id}/0.png"}
    assert await _rows_for_run(run_id) == []
    assert await _used(db_user.id) == 0
    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        assert (run.status, run.error) == ("failed", "storage_quota_exceeded")


async def test_dedup_records_screenshots_but_not_the_duplicate_page(db_user):
    """A dedup'd run points at its predecessor's page and deletes its own copy; the
    screenshots are its own work and stay charged."""
    import xxhash

    content = b"same content"
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        prev_path = f"{BUCKET}/history/{uuid.uuid4()}/scrape.html"
        prev = JobRun(
            job_id=job.id,
            status="completed",
            result_path=prev_path,
            content_hash=xxhash.xxh64(content).hexdigest()[:16],
            completed_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        run = JobRun(job_id=job.id, status="running")
        db.add_all([prev, run])
        db.add(UserQuota(user_id=db_user.id, storage_bytes_limit=10_000, storage_bytes_used=0))
        await db.commit()
        run_id = run.id

    page = f"{BUCKET}/history/{run_id}/scrape.html"
    shot = f"{BUCKET}/screenshots/{run_id}/0.png"
    minio = _minio_with_sizes(
        {f"history/{run_id}/scrape.html": 100, f"screenshots/{run_id}/0.png": 30}, content
    )

    await _handle_result(
        _msg(run_id, "completed", page, screenshot_paths=[shot]), AsyncMock(), minio
    )

    async with AsyncSessionLocal() as db:
        r = await db.get(JobRun, run_id)
        assert r.status == "completed" and r.diff_detected is False
        assert r.result_path == prev_path
    rows = await _rows_for_run(run_id)
    assert {(x.object_key, x.bytes) for x in rows} == {(shot, 30)}
    assert await _used(db_user.id) == 30
    removed = {call.args[1] for call in minio.remove_object.call_args_list}
    assert removed == {f"history/{run_id}/scrape.html"}


async def test_accounting_failure_discards_the_objects(db_user):
    """If the ledger write fails, the run fails *and* the page and screenshots are
    removed. Leaving them would hand the reconcile sweep an attributable, unreachable
    object to charge the user for."""
    from unittest.mock import patch

    _, run_id = await _make_plain_job(db_user.id)
    page = f"{BUCKET}/history/{run_id}/scrape.html"
    shot = f"{BUCKET}/screenshots/{run_id}/0.png"
    minio = _minio_with_sizes(
        {f"history/{run_id}/scrape.html": 100, f"screenshots/{run_id}/0.png": 30}
    )

    with patch("app.core.result_consumer.record_object", side_effect=Exception("db error")):
        await _handle_result(
            _msg(run_id, "completed", page, screenshot_paths=[shot]), AsyncMock(), minio
        )

    removed = {call.args[1] for call in minio.remove_object.call_args_list}
    assert removed == {f"history/{run_id}/scrape.html", f"screenshots/{run_id}/0.png"}
    assert await _rows_for_run(run_id) == []
    assert await _used(db_user.id) == 0
    async with AsyncSessionLocal() as db:
        run = await db.get(JobRun, run_id)
        assert (run.status, run.error) == ("failed", "storage_accounting_failed")


# ---------------------------------------------------------------------------
# Release — the only way out, and what happens when the object store says no
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_headers(mock_clerk_auth):
    return {"Authorization": "Bearer fake.jwt.token"}


async def test_release_failure_keeps_the_row_and_refuses_the_delete(
    client, auth_headers, mock_jetstream
):
    """If an object cannot be removed, its row stays (the bytes are still held), the
    counter does not move for it, and the job is not deleted — a cascade would have
    dropped the row for an object still on disk. What *was* freed stays freed."""
    resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=auth_headers)
    job_id = uuid.UUID(resp.json()["id"])
    async with AsyncSessionLocal() as db:
        run = await db.scalar(select(JobRun).where(JobRun.job_id == job_id))
        job = await db.get(Job, job_id)
        good = f"{BUCKET}/history/{run.id}/scrape.html"
        bad = f"{BUCKET}/history/{run.id}/llm.json"
        db.add_all(
            [
                StorageObject(user_id=job.user_id, object_key=good, bytes=100, job_run_id=run.id),
                StorageObject(user_id=job.user_id, object_key=bad, bytes=7, job_run_id=run.id),
            ]
        )
        quota = await db.get(UserQuota, job.user_id)
        if quota is None:
            db.add(UserQuota(user_id=job.user_id, storage_bytes_used=107))
        else:
            quota.storage_bytes_used = 107
        await db.commit()
        run_id, user_id = run.id, job.user_id

    minio = AsyncMock()

    async def _remove(bucket, key):
        if key.endswith("llm.json"):
            raise Exception("minio down")

    minio.remove_object = AsyncMock(side_effect=_remove)
    app.dependency_overrides[get_minio] = lambda: minio
    try:
        resp = await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_minio, None)

    assert resp.status_code == 503
    async with AsyncSessionLocal() as db:
        assert await db.get(Job, job_id) is not None
    rows = await _rows_for_run(run_id)
    assert [(r.object_key, r.bytes) for r in rows] == [(bad, 7)]
    assert await _used(user_id) == 7

    # The mock-Clerk user persists across the suite; take the kept job with us.
    async with AsyncSessionLocal() as db:
        await db.delete(await db.get(Job, job_id))
        await db.commit()


async def test_pre_ledger_run_falls_back_to_result_path(client, auth_headers, mock_jetstream):
    """A run deployed before the ledger has no rows and the old stamp. Its result_path
    object is removed and decremented by its stat size — what the delete did before P8.
    A run with result_path but no stamp (a dedup'd run pointing at another run's object)
    is left alone."""
    resp = await client.post("/jobs", json={"url": "https://example.com"}, headers=auth_headers)
    job_id = uuid.UUID(resp.json()["id"])
    async with AsyncSessionLocal() as db:
        run = await db.scalar(select(JobRun).where(JobRun.job_id == job_id))
        job = await db.get(Job, job_id)
        legacy_path = f"{BUCKET}/history/{job_id}/1777000000.html"
        run.result_path = legacy_path
        run.status = "completed"
        run.storage_accounted_at = datetime.now(UTC)
        dedup = JobRun(job_id=job_id, status="completed", result_path=legacy_path)
        db.add(dedup)
        quota = await db.get(UserQuota, job.user_id)
        if quota is None:
            db.add(UserQuota(user_id=job.user_id, storage_bytes_used=500))
        else:
            quota.storage_bytes_used = 500
        await db.commit()
        user_id = job.user_id

    minio = _minio_with_sizes({f"history/{job_id}/1777000000.html": 300})
    app.dependency_overrides[get_minio] = lambda: minio
    try:
        resp = await client.delete(f"/jobs/{job_id}?permanent=true", headers=auth_headers)
        assert resp.status_code == 204
    finally:
        app.dependency_overrides.pop(get_minio, None)

    # Removed once (the stamped run), not twice (the dedup'd run shares the key).
    assert minio.remove_object.call_count == 1
    assert await _used(user_id) == 200


# ---------------------------------------------------------------------------
# Schema — the CHECK is closed on purpose, and loud
# ---------------------------------------------------------------------------


async def test_one_producer_check_rejects_a_row_with_none(db_user):
    async with AsyncSessionLocal() as db:
        db.add(StorageObject(user_id=db_user.id, object_key=f"{BUCKET}/x/{uuid.uuid4()}", bytes=1))
        with pytest.raises(IntegrityError, match="ck_storage_objects_one_producer"):
            await db.commit()


# ---------------------------------------------------------------------------
# Nightly cleanup — releases through the ledger, keeps what it could not free
# ---------------------------------------------------------------------------


async def test_cleanup_releases_ledger_rows_and_keeps_failed_runs(db_user):
    import importlib.util

    spec = importlib.util.spec_from_file_location("cleanup", "scripts/cleanup_old_runs.py")
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)

    # Runs dated 2000 against a 2001 cutoff: only this test's rows qualify. A "now - 400d"
    # cutoff would sweep every old run in the shared dev database — it did, once.
    old = datetime(2000, 1, 1, tzinfo=UTC)
    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        ok_run = JobRun(job_id=job.id, status="completed", created_at=old)
        bad_run = JobRun(job_id=job.id, status="completed", created_at=old)
        db.add_all([ok_run, bad_run])
        await db.flush()
        ok_key = f"{BUCKET}/history/{ok_run.id}/scrape.html"
        bad_key = f"{BUCKET}/history/{bad_run.id}/scrape.html"
        db.add_all(
            [
                StorageObject(
                    user_id=db_user.id, object_key=ok_key, bytes=100, job_run_id=ok_run.id
                ),
                StorageObject(
                    user_id=db_user.id, object_key=bad_key, bytes=20, job_run_id=bad_run.id
                ),
            ]
        )
        db.add(UserQuota(user_id=db_user.id, storage_bytes_used=120))
        await db.commit()
        ok_id, bad_id = ok_run.id, bad_run.id

    minio = AsyncMock()

    async def _remove(bucket, key):
        if str(bad_id) in key:
            raise Exception("minio down")

    minio.remove_object = AsyncMock(side_effect=_remove)

    cutoff = datetime(2001, 1, 1, tzinfo=UTC)
    async with AsyncSessionLocal() as db:
        await cleanup._cleanup_loop(db, minio, cutoff)

    async with AsyncSessionLocal() as db:
        assert await db.get(JobRun, ok_id) is None
        assert await db.get(JobRun, bad_id) is not None  # retried next night
    assert await _rows_for_run(ok_id) == []
    assert [r.object_key for r in await _rows_for_run(bad_id)] == [bad_key]
    assert await _used(db_user.id) == 20


# ---------------------------------------------------------------------------
# Reconcile — attribution across both path conventions
# ---------------------------------------------------------------------------


async def test_reconcile_attributes_both_conventions_and_flags_orphans(db_user):
    import importlib.util

    spec = importlib.util.spec_from_file_location("rec", "scripts/reconcile_storage_ledger.py")
    rec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rec)

    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com")
        db.add(job)
        await db.flush()
        new_run = JobRun(job_id=job.id, status="completed")
        old_run = JobRun(
            job_id=job.id,
            status="completed",
            result_path=f"{BUCKET}/history/{job.id}/1777000000.json",
        )
        db.add_all([new_run, old_run])
        await db.commit()
        new_id, job_id = new_run.id, job.id

    async with AsyncSessionLocal() as db:
        new_style = await rec._attribute(
            db, f"{BUCKET}/history/{new_id}/scrape.html", f"history/{new_id}/scrape.html"
        )
        old_style = await rec._attribute(
            db, f"{BUCKET}/history/{job_id}/1777000000.json", f"history/{job_id}/1777000000.json"
        )
        # The leaked page of an old-convention LLM job: same job, a key nothing points at.
        orphan = await rec._attribute(
            db, f"{BUCKET}/history/{job_id}/1776999990.html", f"history/{job_id}/1776999990.html"
        )

    assert (new_style.user_id, new_style.job_run_id) == (db_user.id, new_id)
    assert old_style.user_id == db_user.id and old_style.job_run_id is not None
    assert orphan is None
