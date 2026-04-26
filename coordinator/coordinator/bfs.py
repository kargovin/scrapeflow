"""
BFS dispatch loop for the coordinator.

Polls crawl_queue for pending items, creates crawl_pages rows, dispatches
fat NATS messages to the appropriate worker subject, and detects crawl completion.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update

from coordinator.config import settings
from coordinator.constants import NATS_JOBS_RUN_HTTP_SUBJECT, NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT
from coordinator.db import AsyncSessionLocal
from coordinator.models import Crawl, CrawlPage, CrawlQueueItem, WebhookDelivery

log = structlog.get_logger()

_ACTIVE_STATUSES = frozenset({"queued", "running"})
_TERMINAL_QUEUE_STATUSES = frozenset({"completed", "failed", "skipped"})


def _enqueue_crawl_webhook(db, crawl: Crawl) -> None:
    """Insert a pending WebhookDelivery row. Caller owns the transaction."""
    if not crawl.webhook_url:
        return
    db.add(WebhookDelivery(
        crawl_id=crawl.id,
        webhook_url=crawl.webhook_url,
        payload={
            "event": "crawl.completed",
            "crawl_id": str(crawl.id),
            "status": crawl.status,
            "total_queued": crawl.total_queued,
            "total_completed": crawl.total_completed,
            "total_failed": crawl.total_failed,
            "completed_at": crawl.completed_at.isoformat() if crawl.completed_at else None,
        },
        status="pending",
        next_attempt_at=datetime.now(UTC),
    ))


async def _check_completion(db, crawl: Crawl) -> bool:
    """Return True and mark completed if no pending or dispatched queue items remain."""
    active_count = await db.scalar(
        select(func.count())
        .select_from(CrawlQueueItem)
        .where(
            CrawlQueueItem.crawl_id == crawl.id,
            CrawlQueueItem.status.not_in(list(_TERMINAL_QUEUE_STATUSES)),
        )
    )
    if active_count == 0:
        crawl.status = "completed"
        crawl.completed_at = datetime.now(UTC)
        log.info(
            "crawl_completed",
            crawl_id=str(crawl.id),
            total_completed=crawl.total_completed,
            total_failed=crawl.total_failed,
        )
        return True
    return False


async def reenqueue_stalled(db) -> None:
    """Reset dispatched items older than stale_threshold back to pending on startup."""
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.stale_threshold_minutes)
    result = await db.execute(
        update(CrawlQueueItem)
        .where(
            CrawlQueueItem.status == "dispatched",
            CrawlQueueItem.dispatched_at < cutoff,
        )
        .values(status="pending", dispatched_at=None, crawl_page_id=None)
    )
    if result.rowcount:
        log.info("stalled_items_reenqueued", count=result.rowcount)


async def dispatch_loop(js) -> None:
    """Poll pending queue items and dispatch them to workers. Runs until cancelled."""
    log.info("dispatch_loop_started", batch_size=settings.dispatch_batch_size)
    while True:
        try:
            await asyncio.sleep(settings.dispatch_poll_interval)
            await _dispatch_batch(js)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("dispatch_loop_error", error=str(exc))


async def _dispatch_batch(js) -> None:
    dispatched_crawl_ids: set[uuid.UUID] = set()

    async with AsyncSessionLocal() as db:
        pending = (
            await db.execute(
                select(CrawlQueueItem)
                .where(CrawlQueueItem.status == "pending")
                .order_by(CrawlQueueItem.created_at)
                .limit(settings.dispatch_batch_size)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()

        if not pending:
            await _check_running_crawls_completion(db)
            await db.commit()
            return

        for item in pending:
            crawl = await db.get(Crawl, item.crawl_id)
            if crawl is None or crawl.status not in _ACTIVE_STATUSES:
                item.status = "skipped"
                item.completed_at = datetime.now(UTC)
                continue

            # Transition queued → running on first dispatch (seed URL).
            if crawl.status == "queued":
                crawl.status = "running"
                crawl.total_queued = 1

            page = CrawlPage(
                crawl_id=item.crawl_id,
                url=item.url,
                depth=item.depth,
                status="pending",
            )
            db.add(page)
            await db.flush()  # populate page.id before FK reference

            now = datetime.now(UTC)
            item.status = "dispatched"
            item.crawl_page_id = page.id
            item.dispatched_at = now

            subject = NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT if crawl.engine == "playwright" else NATS_JOBS_RUN_HTTP_SUBJECT
            payload = json.dumps({
                "schema_version": 2,
                "job_id": str(page.id),   # crawl_page_id used as job_id for MinIO path
                "run_id": str(uuid.uuid4()),
                "url": item.url,
                "output_format": "html",   # always html so link extraction can parse it
                "engine": crawl.engine,
                "credentials": None,
                "options": {
                    "respect_robots": crawl.respect_robots,
                    "actions": None,
                },
                "crawl_context": {
                    "crawl_id": str(crawl.id),
                    "crawl_page_id": str(page.id),
                    "depth": item.depth,
                },
            }).encode()

            await js.publish(subject, payload)
            dispatched_crawl_ids.add(crawl.id)
            log.info(
                "crawl_page_dispatched",
                crawl_id=str(crawl.id),
                url=item.url,
                depth=item.depth,
                subject=subject,
            )

        await db.commit()

    # Completion check runs in a fresh session after commit so it sees updated statuses.
    async with AsyncSessionLocal() as db:
        for crawl_id in dispatched_crawl_ids:
            crawl = await db.get(Crawl, crawl_id)
            if crawl and crawl.status == "running":
                completed = await _check_completion(db, crawl)
                if completed:
                    _enqueue_crawl_webhook(db, crawl)
        await db.commit()


async def _check_running_crawls_completion(db) -> None:
    """Scan all running crawls and mark any with no active queue items as completed."""
    running = (
        await db.execute(select(Crawl).where(Crawl.status == "running"))
    ).scalars().all()

    for crawl in running:
        completed = await _check_completion(db, crawl)
        if completed:
            _enqueue_crawl_webhook(db, crawl)
