"""A crawl's counters and its ``crawl_status`` notify, for the v2 lane and the cancel route.

As ``batch_progress``: the page counters are recounted from ``crawl_pages``, never incremented —
the activities run at least once. Lock the crawl row first; every frontier write takes it, so
admission's ``max_pages`` cap and the counters see each other's commits.
"""

import json

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crawl import Crawl, CrawlPage

TERMINAL = frozenset({"completed", "failed", "cancelled"})
CHANNEL = "crawl_status"


async def lock(db: AsyncSession, crawl_id) -> Crawl | None:
    return await db.scalar(
        select(Crawl)
        .where(Crawl.id == crawl_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def recount(db: AsyncSession, crawl: Crawl) -> None:
    await db.flush()
    completed, failed = (
        await db.execute(
            select(
                func.count().filter(CrawlPage.status == "completed"),
                func.count().filter(CrawlPage.status == "failed"),
            ).where(CrawlPage.crawl_id == crawl.id)
        )
    ).one()
    crawl.total_completed, crawl.total_failed = completed, failed


async def notify(
    db: AsyncSession, crawl: Crawl, page_url: str | None = None, page_status: str | None = None
) -> None:
    payload = {
        "crawl_id": str(crawl.id),
        "status": crawl.status,
        "total_queued": crawl.total_queued,
        "total_completed": crawl.total_completed,
        "total_failed": crawl.total_failed,
    }
    if page_url is not None:
        payload |= {"page_url": page_url, "page_status": page_status}
    await db.execute(text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL, "p": json.dumps(payload)})
