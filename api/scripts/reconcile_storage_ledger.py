"""Reconcile the storage ledger against the bucket (ADR-009 §8d's "auditor").

The ledger (`storage_objects`, backlog P8) starts empty: nothing written before it
existed has a row. This script walks the bucket once and makes the three things that
should agree — objects on disk, ledger rows, and `user_quotas.storage_bytes_used` —
actually agree. It is idempotent; a second run finds nothing to do.

Every object under `history/` and `screenshots/` is classified:

  attributable — its key names a row that exists: `history/{artifact_id}/…` where
                 artifact_id is a job_runs.id (job + batch) or a crawl_pages.id (crawl,
                 ADR-011 §2); or, for the pre-ADR-011 `history/{job_id}/{ts}.{ext}`
                 shape, a key some job_runs.result_path still points at.
                 → a ledger row is inserted if missing.
  orphan       — nothing in the database references it. Under the old convention this
                 is BUG-007's leaked scraped page: `result_path` was repointed at the
                 LLM output and the page became unreachable through any API.
                 → deleted, with --apply.

Ledger rows whose object is gone are dropped, rows whose recorded size disagrees with the
listing are corrected, and every user's counter is then set to the sum of their rows. **The counter is recomputed, never adjusted per object**, so an
already-inflated counter (BUG-007's first symptom) is corrected rather than nudged.

⚠️ This deletes production objects and rewrites counters. It is the owner's to run:
nothing invokes it automatically, and without `--apply` it only reports.

Usage (from ./docker):

    docker compose exec api uv run python scripts/reconcile_storage_ledger.py
    docker compose exec api uv run python scripts/reconcile_storage_ledger.py --apply
"""

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.minio import close_client, create_client
from app.models.batch import Batch, BatchItem
from app.models.crawl import Crawl, CrawlPage
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.storage_object import StorageObject
from app.settings import settings

logger = structlog.get_logger()

PREFIXES = ("history/", "screenshots/")


@dataclass
class Attribution:
    user_id: uuid.UUID
    job_run_id: uuid.UUID | None = None
    crawl_page_id: uuid.UUID | None = None


@dataclass
class Report:
    scanned: int = 0
    already_recorded: int = 0
    recorded: int = 0
    recorded_bytes: int = 0
    orphans: int = 0
    orphan_bytes: int = 0
    dangling_rows: int = 0
    resized_rows: int = 0
    counters_changed: int = 0
    failed: int = 0
    orphan_keys: list[str] = field(default_factory=list)
    # Dry run only: bytes each user's counter would move by, from the rows this run
    # did not write. The counter preview reads the ledger, and on a first run the
    # ledger is empty, so without this the preview says "→ 0" for everyone.
    pending_delta: dict[uuid.UUID, int] = field(default_factory=dict)

    def defer(self, user_id: uuid.UUID, delta: int) -> None:
        self.pending_delta[user_id] = self.pending_delta.get(user_id, 0) + delta


def _artifact_id(key: str) -> uuid.UUID | None:
    """The first path segment after the prefix, if it is a UUID. Both conventions put
    an id there; only the new one puts a row id there."""
    parts = key.split("/")
    if len(parts) < 3:
        return None
    try:
        return uuid.UUID(parts[1])
    except ValueError:
        return None


async def _owner_of_run(db: AsyncSession, run_id: uuid.UUID) -> uuid.UUID | None:
    return await db.scalar(
        select(func.coalesce(Job.user_id, Batch.user_id))
        .select_from(JobRun)
        .outerjoin(Job, JobRun.job_id == Job.id)
        .outerjoin(BatchItem, JobRun.batch_item_id == BatchItem.id)
        .outerjoin(Batch, BatchItem.batch_id == Batch.id)
        .where(JobRun.id == run_id)
    )


async def _attribute(db: AsyncSession, path: str, key: str) -> Attribution | None:
    """Find the row that produced `key`, or None if nothing references it."""
    artifact_id = _artifact_id(key)
    if artifact_id is not None:
        owner = await _owner_of_run(db, artifact_id)
        if owner is not None:
            return Attribution(user_id=owner, job_run_id=artifact_id)
        owner = await db.scalar(
            select(Crawl.user_id)
            .select_from(CrawlPage)
            .join(Crawl, CrawlPage.crawl_id == Crawl.id)
            .where(CrawlPage.id == artifact_id)
        )
        if owner is not None:
            return Attribution(user_id=owner, crawl_page_id=artifact_id)

    # Pre-ADR-011 shape: the segment is a job id, not a run id. A dedup'd run shares
    # its predecessor's result_path, so take the earliest run that points here.
    run_id = await db.scalar(
        select(JobRun.id).where(JobRun.result_path == path).order_by(JobRun.created_at).limit(1)
    )
    if run_id is not None:
        owner = await _owner_of_run(db, run_id)
        if owner is not None:
            return Attribution(user_id=owner, job_run_id=run_id)
    return None


async def _walk_bucket(db: AsyncSession, minio, apply: bool, report: Report) -> None:
    bucket = settings.minio_bucket
    for prefix in PREFIXES:
        for obj in await minio.list_objects(bucket, prefix=prefix, recursive=True):
            key = obj.object_name
            path = f"{bucket}/{key}"
            size = obj.size or 0
            report.scanned += 1

            row = await db.scalar(select(StorageObject).where(StorageObject.object_key == path))
            if row is not None:
                report.already_recorded += 1
                # A row recorded while stat_object was failing holds 0 (UF-003 3b); the
                # listing is authoritative for size, and the counter is recomputed below.
                if row.bytes != size:
                    report.resized_rows += 1
                    logger.info(
                        "reconcile: size " + ("corrected" if apply else "would correct"),
                        key=key,
                        recorded=row.bytes,
                        actual=size,
                    )
                    if apply:
                        row.bytes = size
                    else:
                        report.defer(row.user_id, size - row.bytes)
                continue

            attribution = await _attribute(db, path, key)
            if attribution is None:
                report.orphans += 1
                report.orphan_bytes += size
                report.orphan_keys.append(key)
                if apply:
                    try:
                        await minio.remove_object(bucket, key)
                        logger.info("reconcile: orphan deleted", key=key, bytes=size)
                    except Exception:
                        report.failed += 1
                        logger.exception("reconcile: orphan delete failed", key=key)
                else:
                    logger.info("reconcile: orphan (would delete)", key=key, bytes=size)
                continue

            report.recorded += 1
            report.recorded_bytes += size
            if apply:
                db.add(
                    StorageObject(
                        user_id=attribution.user_id,
                        object_key=path,
                        bytes=size,
                        job_run_id=attribution.job_run_id,
                        crawl_page_id=attribution.crawl_page_id,
                    )
                )
                logger.info(
                    "reconcile: recorded",
                    key=key,
                    bytes=size,
                    user_id=str(attribution.user_id),
                )
            else:
                report.defer(attribution.user_id, size)
                logger.info(
                    "reconcile: unrecorded (would record)",
                    key=key,
                    bytes=size,
                    user_id=str(attribution.user_id),
                )
    if apply:
        await db.flush()


async def _drop_dangling_rows(db: AsyncSession, minio, apply: bool, report: Report) -> None:
    """A row whose object is gone charges the user for nothing."""
    bucket = settings.minio_bucket
    rows = (await db.execute(select(StorageObject))).scalars().all()
    for row in rows:
        _, _, key = row.object_key.partition("/")
        try:
            await minio.stat_object(bucket, key)
            continue
        except Exception as exc:  # S3Error NoSuchKey, or unreachable — only the former is dangling
            if getattr(exc, "code", None) != "NoSuchKey":
                report.failed += 1
                logger.warning("reconcile: stat failed, row kept", key=key, error=str(exc))
                continue
        report.dangling_rows += 1
        if apply:
            await db.delete(row)
            logger.info("reconcile: dangling row dropped", key=key, bytes=row.bytes)
        else:
            report.defer(row.user_id, -row.bytes)
            logger.info("reconcile: dangling row (would drop)", key=key, bytes=row.bytes)
    if apply:
        await db.flush()


async def _recompute_counters(
    db: AsyncSession, apply: bool, report: Report
) -> list[tuple[uuid.UUID, int, int]]:
    """Set every counter to the sum of the user's rows. Users with a quota row but no
    objects go to zero; users with objects but no quota row get one.

    Returns `(user_id, before, after)` for every counter that changes (or would). In a
    dry run the ledger still lacks the rows the walk would have written, so `after` is
    the ledger sum plus `report.pending_delta`; a user with neither a quota row nor a
    ledger row is invisible to the query and is added from the deltas."""
    rows = (
        await db.execute(
            text("""
                SELECT COALESCE(q.user_id, s.user_id) AS user_id,
                       COALESCE(q.storage_bytes_used, 0) AS used,
                       COALESCE(s.total, 0) AS total
                FROM user_quotas q
                FULL OUTER JOIN (
                    SELECT user_id, SUM(bytes) AS total FROM storage_objects GROUP BY user_id
                ) s ON s.user_id = q.user_id
            """)
        )
    ).all()
    totals = {user_id: (int(used), int(total)) for user_id, used, total in rows}
    if not apply:
        for user_id, delta in report.pending_delta.items():
            used, total = totals.get(user_id, (0, 0))
            totals[user_id] = (used, total + delta)
    decisions: list[tuple[uuid.UUID, int, int]] = []
    for user_id, (used, total) in totals.items():
        if used == total:
            continue
        decisions.append((user_id, used, total))
        report.counters_changed += 1
        logger.info(
            "reconcile: counter " + ("set" if apply else "would set"),
            user_id=str(user_id),
            before=used,
            after=total,
        )
        if apply:
            await db.execute(
                text("""
                    INSERT INTO user_quotas (user_id, storage_bytes_used, updated_at)
                    VALUES (:user_id, :total, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET storage_bytes_used = :total, updated_at = NOW()
                """),
                {"user_id": user_id, "total": total},
            )
    return decisions


async def reconcile(db: AsyncSession, minio, apply: bool) -> Report:
    report = Report()
    await _walk_bucket(db, minio, apply, report)
    await _drop_dangling_rows(db, minio, apply, report)
    await _recompute_counters(db, apply, report)
    if apply:
        await db.commit()
    logger.info(
        "reconcile: done",
        mode="apply" if apply else "dry-run",
        scanned=report.scanned,
        already_recorded=report.already_recorded,
        recorded=report.recorded,
        recorded_bytes=report.recorded_bytes,
        orphans=report.orphans,
        orphan_bytes=report.orphan_bytes,
        dangling_rows=report.dangling_rows,
        resized_rows=report.resized_rows,
        counters_changed=report.counters_changed,
        failed=report.failed,
    )
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="record, delete orphans, drop dangling rows and rewrite counters. "
        "Without it the script only reports.",
    )
    args = parser.parse_args()

    if not args.apply:
        logger.warning("reconcile: DRY RUN — nothing will change. Pass --apply to reconcile.")

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    minio = await create_client()
    try:
        async with AsyncSession(engine) as db:
            await reconcile(db, minio, apply=args.apply)
    finally:
        await close_client(minio)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
