"""The storage ledger — per-object accounting (ADR-009 §8d, backlog P8).

Every MinIO object held on a user's behalf is one `storage_objects` row, and
`user_quotas.storage_bytes_used` is a materialised sum of those rows, moved in the
same transaction as every insert and delete here. Two entry points do all the work:

record_object()          — the accountant: one row per stored object, charged once.
                           Keyed on the object key, so a NATS redelivery of the same
                           result is a no-op while a genuinely second artifact for the
                           same run (an LLM job's extraction, a screenshot) is charged.
                           That distinction is what BUG-007 was missing.
release_*()              — the only way objects leave: enumerate rows, remove each
                           object, decrement by the recorded size, delete the row.
                           Nothing here derives a filename from a format or a path.

Pre-ledger runs (deployed before this table existed) have no rows. For those, and only
those, the release helpers fall back to the run's `result_path` gated on the old
`storage_accounted_at` stamp — exactly what the delete paths did before P8, so a
pre-cutover object is never decremented twice and never decremented when it was never
charged. `scripts/reconcile_storage_ledger.py` backfills rows from the bucket and retires
that branch's relevance.

Caller owns the transaction throughout.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog
from miniopy_async import Minio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.quota import decrement_storage_bytes, increment_storage_bytes
from app.core.storage import delete_minio_object, stat_minio_size
from app.models.batch import Batch, BatchItem
from app.models.crawl import CrawlPage
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.storage_object import StorageObject

logger = structlog.get_logger()


async def record_object(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    object_key: str,
    size: int,
    job_run_id: uuid.UUID | None = None,
    crawl_page_id: uuid.UUID | None = None,
) -> bool:
    """Record one stored object and charge its bytes. Returns True if the row is new.

    Idempotent on `object_key`: a second call for the same key changes nothing and
    returns False. The counter moves only when the row is inserted, in this statement's
    transaction, so the ledger and the counter cannot disagree on a commit boundary.
    """
    inserted = await db.scalar(
        text("""
            INSERT INTO storage_objects
                (id, user_id, object_key, bytes, job_run_id, crawl_page_id, created_at)
            VALUES (:id, :user_id, :object_key, :bytes, :job_run_id, :crawl_page_id, NOW())
            ON CONFLICT (object_key) DO NOTHING
            RETURNING id
        """),
        {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "object_key": object_key,
            "bytes": size,
            "job_run_id": job_run_id,
            "crawl_page_id": crawl_page_id,
        },
    )
    if inserted is None:
        logger.info("storage_object_already_recorded", object_key=object_key)
        return False
    if size > 0:
        await increment_storage_bytes(user_id, db, size)
    return True


@dataclass
class ReleaseOutcome:
    """What a release pass did. `failed_run_ids` lets a caller skip runs whose objects
    are still on disk — those rows are kept so the next attempt sees them."""

    released: int = 0
    freed_bytes: int = 0
    failed: int = 0
    failed_run_ids: set[uuid.UUID] = field(default_factory=set)

    def merge(self, other: "ReleaseOutcome") -> None:
        self.released += other.released
        self.freed_bytes += other.freed_bytes
        self.failed += other.failed
        self.failed_run_ids |= other.failed_run_ids


async def objects_for_runs(db: AsyncSession, run_ids: Sequence[uuid.UUID]) -> list[StorageObject]:
    if not run_ids:
        return []
    result = await db.execute(select(StorageObject).where(StorageObject.job_run_id.in_(run_ids)))
    return list(result.scalars().all())


async def objects_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[StorageObject]:
    result = await db.execute(select(StorageObject).where(StorageObject.user_id == user_id))
    return list(result.scalars().all())


async def objects_for_crawl(db: AsyncSession, crawl_id: uuid.UUID) -> list[StorageObject]:
    """Every object any page of the crawl produced."""
    page_ids = select(CrawlPage.id).where(CrawlPage.crawl_id == crawl_id)
    result = await db.execute(
        select(StorageObject).where(StorageObject.crawl_page_id.in_(page_ids))
    )
    return list(result.scalars().all())


async def release_objects(
    db: AsyncSession, minio: Minio, rows: Sequence[StorageObject], label: str
) -> ReleaseOutcome:
    """Remove each row's object, decrement by its recorded size, delete the row.

    A row whose object could not be removed is left in place — the object is still on
    disk, so the user is still holding those bytes. The caller decides what that means
    for the parent row (a delete endpoint refuses; the nightly cleanup retries).
    """
    outcome = ReleaseOutcome()
    for row in rows:
        if not await delete_minio_object(minio, row.object_key, label):
            outcome.failed += 1
            if row.job_run_id is not None:
                outcome.failed_run_ids.add(row.job_run_id)
            continue
        if row.bytes > 0:
            await decrement_storage_bytes(row.user_id, db, row.bytes)
        await db.delete(row)
        outcome.released += 1
        outcome.freed_bytes += row.bytes
    return outcome


async def _release_legacy_result(
    db: AsyncSession, minio: Minio, run: JobRun, user_id: uuid.UUID, label: str
) -> bool:
    """Pre-ledger fallback: the run's `result_path` object, decremented only if the old
    per-run stamp says it was charged. Never reached for a run that has ledger rows,
    because the stamp is not written any more."""
    size = await stat_minio_size(minio, run.result_path)
    if not await delete_minio_object(minio, run.result_path, f"{label} (pre-ledger)"):
        return False
    if size > 0:
        await decrement_storage_bytes(user_id, db, size)
    return True


def _is_legacy_run(run: JobRun) -> bool:
    return run.storage_accounted_at is not None and run.result_path is not None


async def release_run_objects(
    db: AsyncSession,
    minio: Minio,
    runs: Sequence[JobRun],
    user_id: uuid.UUID,
    label: str,
) -> ReleaseOutcome:
    """Release everything the given runs (all owned by `user_id`) hold."""
    outcome = await release_objects(
        db, minio, await objects_for_runs(db, [run.id for run in runs]), label
    )
    for run in runs:
        if _is_legacy_run(run) and not await _release_legacy_result(db, minio, run, user_id, label):
            outcome.failed += 1
            outcome.failed_run_ids.add(run.id)
    return outcome


async def release_crawl_objects(
    db: AsyncSession, minio: Minio, crawl_id: uuid.UUID, label: str
) -> ReleaseOutcome:
    """Release everything a crawl's pages hold. No legacy branch: crawl pages were never
    charged before the ledger, so there is nothing older than a row to fall back to."""
    return await release_objects(db, minio, await objects_for_crawl(db, crawl_id), label)


async def release_user_objects(
    db: AsyncSession, minio: Minio, user_id: uuid.UUID, label: str
) -> ReleaseOutcome:
    """Release everything a user holds, on every lane — the ledger is keyed on the owner,
    so this needs no per-lane arm. The legacy branch is job-lane only by construction."""
    outcome = await release_objects(db, minio, await objects_for_user(db, user_id), label)

    legacy_runs = (
        await db.execute(
            select(JobRun)
            .outerjoin(Job, JobRun.job_id == Job.id)
            .outerjoin(BatchItem, JobRun.batch_item_id == BatchItem.id)
            .outerjoin(Batch, BatchItem.batch_id == Batch.id)
            .where(
                func.coalesce(Job.user_id, Batch.user_id) == user_id,
                JobRun.storage_accounted_at.is_not(None),
                JobRun.result_path.is_not(None),
            )
        )
    ).scalars()
    for run in legacy_runs:
        if not await _release_legacy_result(db, minio, run, user_id, label):
            outcome.failed += 1
            outcome.failed_run_ids.add(run.id)
    return outcome
