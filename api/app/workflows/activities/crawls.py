"""Crawl-lane app-DB activities — the frontier (ADR-009 13b) and the crawl's mirror on ``crawls``.

Every URL that joins a crawl's frontier passes ``admit_urls`` — the seed, extracted links and
sitemap entries alike (13d) — and every sitemap file is vetted by ``vet_urls`` before it is
fetched (ADR-010 §2). ``crawl_queue`` is the queue and the seen-set in one table: its
``UNIQUE (crawl_id, url)`` index is the dedup, and a refused URL stays in it as a ``dropped``
row, so it is never checked or counted twice.

Two writers own ``crawls.status``: the API's cancel and these activities. A cancel wins; an
activity never moves a crawl out of a terminal state, and drops what a stopped crawl fetched.
Every activity locks the crawl row first.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity

from app.core import crawl_progress, webhook_delivery
from app.core.db import AsyncSessionLocal
from app.core.domains import registrable_domain
from app.core.ledger import record_object
from app.core.quota import storage_usage
from app.core.security import validate_no_ssrf_core
from app.core.webhooks import create_crawl_webhook_delivery
from app.models.crawl import Crawl, CrawlPage, CrawlQueueItem
from app.workflows.activities.job_runs import GONE, discard_unrecorded
from app.workflows.crawl import (
    ADMIT_URLS_ACTIVITY,
    CLAIM_CRAWL_PAGES_ACTIVITY,
    CRAWL_STARTED_ACTIVITY,
    FINISH_CRAWL_ACTIVITY,
    RECORD_CRAWL_PAGE_ACTIVITY,
    VET_URLS_ACTIVITY,
    Admitted,
    Claim,
    ClaimedPage,
    ClaimInput,
    CrawlRef,
    FinishCrawlInput,
    PageRecorded,
    RecordCrawlPageInput,
    UrlBatch,
    Vetted,
)

logger = structlog.get_logger()

# A page still running once the workflow has finished: claimed by a retried claim, never fetched.
NO_OUTCOME = "internal error: the page was never fetched"
_PAGE_TERMINAL = ("completed", "failed", "cancelled")


def _normalise(url: str) -> str | None:
    """The URL as the frontier keys it (fragment dropped), or None if it is not http(s)."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
    except ValueError:
        return None
    return urlunsplit(parts._replace(fragment=""))


def _filtered(url: str, crawl: Crawl) -> bool:
    # The include/exclude prefixes link_extractor applied on v1, now on every discovered URL.
    path = urlsplit(url).path or "/"
    if crawl.include_paths and not any(path.startswith(p) for p in crawl.include_paths):
        return True
    return bool(crawl.exclude_paths and any(path.startswith(p) for p in crawl.exclude_paths))


class _Gate:
    """Admission's rules for one call. Host lookups are cached for the call only (DNS rebinding)."""

    def __init__(self, crawl: Crawl) -> None:
        self.crawl = crawl
        self.scope = registrable_domain(crawl.seed_url)
        self._hosts: dict[str, str | None] = {}

    async def reason(self, url: str, source: str) -> str | None:
        """Why ``url`` (normalised) is refused, or None. ADR-010 §2's order: scope, then SSRF."""
        if source in ("sitemap", "sitemap_file") and registrable_domain(url) != self.scope:
            return "out_of_scope"
        if source in ("link", "sitemap") and _filtered(url, self.crawl):
            return "filtered"
        return await self._ssrf(url)

    async def _ssrf(self, url: str) -> str | None:
        host = urlsplit(url).hostname
        if host not in self._hosts:
            try:
                await asyncio.to_thread(validate_no_ssrf_core, url)
                self._hosts[host] = None
            except ValueError as exc:
                self._hosts[host] = "unresolved" if "could not be resolved" in str(exc) else "ssrf"
        return self._hosts[host]


def _log_drop(crawl: Crawl, url: str, reason: str, source: str) -> None:
    logger.info("crawl_url_dropped", crawl_id=str(crawl.id), url=url, reason=reason, source=source)


def _count(crawl: Crawl, reasons: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    if counts:
        merged = dict(crawl.dropped or {})
        for reason, n in counts.items():
            merged[reason] = merged.get(reason, 0) + n
        crawl.dropped = merged
    return counts


@activity.defn(name=CRAWL_STARTED_ACTIVITY)
async def crawl_started(input: CrawlRef) -> str:
    """queued → running. ``running`` means scheduled, as on the job lane."""
    async with AsyncSessionLocal() as db:
        crawl = await crawl_progress.lock(db, uuid.UUID(input.crawl_id))
        if crawl is None:
            return GONE
        if crawl.status == "queued":
            crawl.status = "running"
            await crawl_progress.notify(db, crawl)
            await db.commit()
        return crawl.status


@activity.defn(name=ADMIT_URLS_ACTIVITY)
async def admit_urls(input: UrlBatch) -> Admitted:
    """Adds the URLs not yet seen to the frontier at ``input.depth``, up to ``max_pages``.

    Refused ones (``invalid``, ``out_of_scope``, ``filtered``, ``ssrf``, ``unresolved``) are kept
    as dropped rows and counted on the crawl; a refusal is not a failure and is never retried.
    Idempotent: a retried call finds its URLs seen.
    """
    async with AsyncSessionLocal() as db:
        crawl = await crawl_progress.lock(db, uuid.UUID(input.crawl_id))
        if crawl is None:
            return Admitted(state=GONE)
        if crawl.status != "running":
            return Admitted(state=crawl.status)
        room = crawl.max_pages - crawl.total_queued
        if room <= 0:
            logger.info("crawl_frontier_full", crawl_id=str(crawl.id), discarded=len(input.urls))
            return Admitted(state=crawl.status)

        # (frontier key, as discovered); an invalid URL is keyed as it came.
        candidates = [(_normalise(u) or u.strip(), u) for u in input.urls]
        keys = list(dict.fromkeys(key for key, _ in candidates))
        seen = set(
            (
                await db.scalars(
                    select(CrawlQueueItem.url).where(
                        CrawlQueueItem.crawl_id == crawl.id, CrawlQueueItem.url.in_(keys)
                    )
                )
            ).all()
        )

        gate = _Gate(crawl)
        fresh: list[str] = []
        refused: list[tuple[str, str]] = []
        for key, raw in candidates:
            if key in seen:
                continue
            seen.add(key)
            reason = "invalid" if _normalise(raw) is None else await gate.reason(key, input.source)
            if reason is None:
                fresh.append(key)
            else:
                refused.append((key, reason))
        over = fresh[room:]
        fresh = fresh[:room]

        now = datetime.now(UTC)
        rows = [{"url": url, "status": "pending", "drop_reason": None} for url in fresh] + [
            {"url": url, "status": "dropped", "drop_reason": reason} for url, reason in refused
        ]
        inserted: list[tuple[str, str | None]] = []
        if rows:
            inserted = (
                await db.execute(
                    pg_insert(CrawlQueueItem)
                    .values(
                        [
                            {
                                "id": uuid.uuid4(),
                                "crawl_id": crawl.id,
                                "depth": input.depth,
                                "created_at": now,
                                **row,
                            }
                            for row in rows
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["crawl_id", "url"])
                    .returning(CrawlQueueItem.url, CrawlQueueItem.drop_reason)
                )
            ).all()
        admitted = sum(1 for _, reason in inserted if reason is None)
        dropped = _count(crawl, [reason for _, reason in inserted if reason is not None])
        crawl.total_queued += admitted
        if admitted:
            await crawl_progress.notify(db, crawl)
        await db.commit()

    for url, reason in inserted:
        if reason is not None:
            _log_drop(crawl, url, reason, input.source)
    if over:
        logger.info("crawl_frontier_full", crawl_id=str(crawl.id), discarded=len(over))
    return Admitted(state="running", admitted=admitted, dropped=dropped)


@activity.defn(name=VET_URLS_ACTIVITY)
async def vet_urls(input: UrlBatch) -> Vetted:
    """The sitemap files that may be fetched: in scope and not internal (ADR-010 §2 amendment).

    Refusals are counted as ``sitemap_<reason>``; a file is not a page, so nothing is stored.
    """
    async with AsyncSessionLocal() as db:
        crawl = await crawl_progress.lock(db, uuid.UUID(input.crawl_id))
        if crawl is None or crawl.status != "running":
            return Vetted()
        gate = _Gate(crawl)
        allowed: list[str] = []
        refused: list[tuple[str, str]] = []
        for raw in dict.fromkeys(input.urls):
            url = _normalise(raw)
            reason = "invalid" if url is None else await gate.reason(url, input.source)
            if reason is None:
                allowed.append(url)
            else:
                refused.append((url or raw, f"sitemap_{reason}"))
        _count(crawl, [reason for _, reason in refused])
        await db.commit()

    for url, reason in refused:
        _log_drop(crawl, url, reason, input.source)
    return Vetted(urls=allowed)


@activity.defn(name=CLAIM_CRAWL_PAGES_ACTIVITY)
async def claim_crawl_pages(input: ClaimInput) -> Claim:
    """The next pages to fetch, breadth first: a ``crawl_pages`` row each (P7's unit), ``running``.

    A page claimed earlier and not among ``in_flight`` is handed back first — a retried claim
    whose answer the workflow never saw. Claims nothing once the owner is at the storage limit.
    """
    async with AsyncSessionLocal() as db:
        crawl = await crawl_progress.lock(db, uuid.UUID(input.crawl_id))
        if crawl is None:
            return Claim(state=GONE)
        if crawl.status != "running":
            return Claim(state=crawl.status)
        used, limit = await storage_usage(crawl.user_id, db)
        if used >= limit:
            return Claim(state=crawl.status, storage_full=True)

        orphaned = [CrawlQueueItem.crawl_id == crawl.id, CrawlQueueItem.status == "dispatched"]
        if input.in_flight:
            orphaned.append(CrawlPage.id.not_in([uuid.UUID(p) for p in input.in_flight]))
        orphans = (
            await db.scalars(
                select(CrawlPage)
                .join(CrawlQueueItem, CrawlQueueItem.crawl_page_id == CrawlPage.id)
                .where(*orphaned)
                .order_by(CrawlPage.created_at)
                .limit(input.limit)
            )
        ).all()
        pages = list(orphans)

        items = (
            await db.scalars(
                select(CrawlQueueItem)
                .where(CrawlQueueItem.crawl_id == crawl.id, CrawlQueueItem.status == "pending")
                .order_by(CrawlQueueItem.depth, CrawlQueueItem.created_at)
                .limit(input.limit - len(pages))
            )
        ).all()
        now = datetime.now(UTC)
        for item in items:
            page = CrawlPage(crawl_id=crawl.id, url=item.url, depth=item.depth, status="running")
            db.add(page)
            await db.flush()
            item.status, item.crawl_page_id, item.dispatched_at = "dispatched", page.id, now
            pages.append(page)
        if items:
            await crawl_progress.notify(db, crawl)
        await db.commit()
        return Claim(
            state=crawl.status,
            pages=[ClaimedPage(page_id=str(p.id), url=p.url, depth=p.depth) for p in pages],
        )


@activity.defn(name=RECORD_CRAWL_PAGE_ACTIVITY)
async def record_crawl_page(input: RecordCrawlPageInput) -> PageRecorded:
    """A page's outcome: a completed page is charged to the crawl's owner, even past the limit
    (§8d — the page that crosses it is kept). On a crawl no longer running the page is cancelled
    and its objects dropped uncharged. Idempotent: a page already recorded is left as it is.
    """
    paths = [o.path for o in input.objects]
    async with AsyncSessionLocal() as db:
        crawl = await crawl_progress.lock(db, uuid.UUID(input.crawl_id))
        page = await db.get(CrawlPage, uuid.UUID(input.page_id), populate_existing=True)
        if crawl is None or page is None or page.crawl_id != crawl.id:
            await discard_unrecorded(db, paths, "crawl object of a deleted crawl")
            return PageRecorded(state=GONE if crawl is None else crawl.status)
        if page.status in _PAGE_TERMINAL:
            used, limit = await storage_usage(crawl.user_id, db)
            return PageRecorded(state=crawl.status, storage_full=used >= limit)

        item = await db.scalar(
            select(CrawlQueueItem).where(CrawlQueueItem.crawl_page_id == page.id)
        )
        if crawl.status != "running" or input.status == "cancelled":
            await discard_unrecorded(db, paths, "crawl object on a stopped crawl")
            page.status, queue_status = "cancelled", "skipped"
        elif input.status == "completed":
            for obj in input.objects:
                await record_object(
                    db,
                    user_id=crawl.user_id,
                    object_key=obj.path,
                    size=obj.size,
                    crawl_page_id=page.id,
                )
            page.status, page.result_path, queue_status = "completed", paths[0], "completed"
        else:
            await discard_unrecorded(db, paths, "crawl object of a failed page")
            page.status, page.error, queue_status = "failed", input.error, "failed"
        now = datetime.now(UTC)
        page.completed_at = now
        if item is not None:
            item.status, item.completed_at = queue_status, now
        await crawl_progress.recount(db, crawl)
        await crawl_progress.notify(db, crawl, page_url=page.url, page_status=page.status)
        await db.commit()
        used, limit = await storage_usage(crawl.user_id, db)
        if used >= limit:
            logger.warning("crawl_storage_limit", crawl_id=str(crawl.id), used=used, limit=limit)
        return PageRecorded(state=crawl.status, storage_full=used >= limit)


@activity.defn(name=FINISH_CRAWL_ACTIVITY)
async def finish_crawl(input: FinishCrawlInput) -> str:
    """Closes the crawl and writes its webhook row in one transaction; returns its status.

    A crawl the API cancelled stays cancelled. What is left of the frontier is skipped; a page
    still running is cancelled with the crawl, else failed.
    """
    async with AsyncSessionLocal() as db:
        crawl = await crawl_progress.lock(db, uuid.UUID(input.crawl_id))
        if crawl is None:
            return GONE
        now = datetime.now(UTC)
        moved = crawl.status in ("queued", "running")
        if moved:
            crawl.status = input.status
            crawl.error = input.error
        if crawl.completed_at is None:
            crawl.completed_at = now

        cancelled = crawl.status == "cancelled"
        await db.execute(
            update(CrawlPage)
            .where(CrawlPage.crawl_id == crawl.id, CrawlPage.status.not_in(_PAGE_TERMINAL))
            .values(
                status="cancelled" if cancelled else "failed",
                error=None if cancelled else NO_OUTCOME,
                completed_at=now,
            )
        )
        await db.execute(
            update(CrawlQueueItem)
            .where(
                CrawlQueueItem.crawl_id == crawl.id,
                CrawlQueueItem.status.in_(("pending", "dispatched")),
            )
            .values(status="skipped", completed_at=now)
        )
        await crawl_progress.recount(db, crawl)
        if moved and not cancelled and crawl.webhook_url:
            await create_crawl_webhook_delivery(db, crawl, event=f"crawl.{crawl.status}")
        await crawl_progress.notify(db, crawl)
        await db.commit()
        if crawl.webhook_url:
            await webhook_delivery.start_pending(activity.client, db, crawl_id=crawl.id)
        logger.info(
            "crawl_finished",
            crawl_id=str(crawl.id),
            status=crawl.status,
            completed=crawl.total_completed,
            failed=crawl.total_failed,
            dropped=crawl.dropped,
        )
        return crawl.status
