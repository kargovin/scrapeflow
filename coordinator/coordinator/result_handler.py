"""
NATS result subscriber for the coordinator.

Pulls from scrapeflow.jobs.result with its own durable consumer group
("coordinator-result-consumer") so every message is delivered independently
of the API consumer ("api-result-consumer").

Routing: messages with crawl_context == null are acked and skipped —
the API result consumer owns those. Messages with crawl_context present
are crawl page results that only the coordinator processes.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import nats
import nats.errors
import structlog
from miniopy_async import Minio
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from coordinator.constants import NATS_JOBS_RESULT_SUBJECT, NATS_STREAM_NAME
from coordinator.db import AsyncSessionLocal
from coordinator.link_extractor import extract_links
from coordinator.models import Crawl, CrawlPage, CrawlQueueItem, WebhookDelivery
from coordinator.sitemap import discover_sitemap_urls

log = structlog.get_logger()

DURABLE_NAME = "coordinator-result-consumer"

_TERMINAL_QUEUE_STATUSES = frozenset({"completed", "failed", "skipped"})


def enqueue_crawl_webhook(db, crawl: Crawl) -> None:
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


async def check_completion(db, crawl: Crawl) -> bool:
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


async def _fetch_minio_bytes(minio: Minio, minio_path: str) -> bytes:
    """Fetch raw bytes from MinIO. minio_path format: 'bucket/object/key'."""
    bucket, _, key = minio_path.partition("/")
    response = await minio.get_object(bucket, key)
    data = await response.read()
    response.close()
    return data


async def _enqueue_url(db, crawl_id: uuid.UUID, url: str, depth: int) -> bool:
    """Insert a URL into crawl_queue, silently ignoring duplicates. Returns True if inserted."""
    stmt = (
        pg_insert(CrawlQueueItem)
        .values(
            id=uuid.uuid4(),
            crawl_id=crawl_id,
            url=url,
            depth=depth,
            status="pending",
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing()
    )
    result = await db.execute(stmt)
    return result.rowcount > 0


async def _process_crawl_result(db, minio: Minio, data: dict) -> None:
    """Apply a single crawl page result to crawl_pages + crawl_queue. Caller commits."""
    ctx = data["crawl_context"]
    crawl_id = uuid.UUID(ctx["crawl_id"])
    crawl_page_id = uuid.UUID(ctx["crawl_page_id"])
    depth = ctx["depth"]
    worker_status = data["status"]
    minio_path = data.get("minio_path")
    error = data.get("error")
    now = datetime.now(UTC)

    crawl = await db.get(Crawl, crawl_id)
    if crawl is None:
        log.warning("crawl_not_found", crawl_id=str(crawl_id))
        return

    page = await db.get(CrawlPage, crawl_page_id)
    if page is None:
        log.warning("crawl_page_not_found", crawl_page_id=str(crawl_page_id))
        return

    if worker_status == "running":
        page.status = "running"
        return

    # Terminal result — update crawl_queue item that was linked to this page.
    queue_terminal = "completed" if worker_status == "completed" else "failed"
    await db.execute(
        update(CrawlQueueItem)
        .where(CrawlQueueItem.crawl_page_id == crawl_page_id)
        .values(status=queue_terminal, completed_at=now)
    )

    if worker_status == "completed":
        page.status = "completed"
        page.result_path = minio_path
        page.completed_at = now
        crawl.total_completed += 1

        next_depth = depth + 1
        if minio_path and next_depth <= crawl.max_depth:
            try:
                html = (await _fetch_minio_bytes(minio, minio_path)).decode(
                    "utf-8", errors="replace"
                )
                new_links = extract_links(
                    html, page.url, crawl.include_paths, crawl.exclude_paths
                )
                for link_url in new_links:
                    if crawl.total_queued >= crawl.max_pages:
                        break
                    inserted = await _enqueue_url(db, crawl_id, link_url, next_depth)
                    if inserted:
                        crawl.total_queued += 1
            except Exception as exc:
                log.error(
                    "link_extraction_failed",
                    crawl_page_id=str(crawl_page_id),
                    error=str(exc),
                )

        # Sitemap discovery only on the seed URL (depth == 0).
        if depth == 0 and not crawl.ignore_sitemap:
            try:
                sitemap_urls = await discover_sitemap_urls(crawl.seed_url)
                for url in sitemap_urls:
                    if crawl.total_queued >= crawl.max_pages:
                        break
                    inserted = await _enqueue_url(db, crawl_id, url, 1)
                    if inserted:
                        crawl.total_queued += 1
            except Exception as exc:
                log.error("sitemap_discovery_failed", crawl_id=str(crawl_id), error=str(exc))

    else:
        # worker_status == "failed"
        page.status = "failed"
        page.error = error
        page.completed_at = now
        crawl.total_failed += 1

    if crawl.status == "running":
        completed = await check_completion(db, crawl)
        if completed:
            enqueue_crawl_webhook(db, crawl)


async def result_handler_loop(js, minio: Minio) -> None:
    """Pull crawl result messages and process them. Runs until cancelled."""
    sub = await js.pull_subscribe(NATS_JOBS_RESULT_SUBJECT, durable=DURABLE_NAME, stream=NATS_STREAM_NAME)
    log.info("result_handler_subscribed", subject=NATS_JOBS_RESULT_SUBJECT, durable=DURABLE_NAME)

    backoff = 2
    while True:
        try:
            msgs = await sub.fetch(batch=10, timeout=5)
            backoff = 2
        except nats.errors.TimeoutError:
            continue
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("result_fetch_error", error=str(exc), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for msg in msgs:
            try:
                data = json.loads(msg.data.decode())

                if data.get("crawl_context") is None:
                    await msg.ack()
                    continue

                async with AsyncSessionLocal() as db:
                    await _process_crawl_result(db, minio, data)
                    await db.commit()

                await msg.ack()
                ctx = data["crawl_context"]
                log.info(
                    "crawl_result_processed",
                    crawl_id=ctx.get("crawl_id"),
                    crawl_page_id=ctx.get("crawl_page_id"),
                    status=data.get("status"),
                )

            except Exception as exc:
                log.error("result_handler_error", error=str(exc), data=str(msg.data[:200]))
                try:
                    await msg.nak()
                except Exception:
                    pass
