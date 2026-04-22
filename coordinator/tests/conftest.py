"""
Shared fixtures and helpers for coordinator unit tests.

No live connections — all infrastructure is mocked.

ORM model instances are built as MagicMock objects rather than real SQLAlchemy
instances. __new__() bypasses the mapper init, leaving instrumented attributes
in a broken state. MagicMock acts like a plain Python namespace — any attribute
can be freely read and written, which is exactly what the coordinator code does.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock


def make_crawl(
    *,
    crawl_id: uuid.UUID | None = None,
    status: str = "running",
    max_depth: int = 3,
    max_pages: int = 100,
    engine: str = "http",
    respect_robots: bool = False,
    ignore_sitemap: bool = True,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    webhook_url: str | None = None,
    seed_url: str = "https://example.com",
    total_queued: int = 1,
    total_completed: int = 0,
    total_failed: int = 0,
) -> MagicMock:
    """Return a mock object that behaves like a Crawl ORM row."""
    crawl = MagicMock()
    crawl.id = crawl_id or uuid.uuid4()
    crawl.seed_url = seed_url
    crawl.status = status
    crawl.max_depth = max_depth
    crawl.max_pages = max_pages
    crawl.engine = engine
    crawl.respect_robots = respect_robots
    crawl.ignore_sitemap = ignore_sitemap
    crawl.include_paths = include_paths
    crawl.exclude_paths = exclude_paths
    crawl.webhook_url = webhook_url
    crawl.total_queued = total_queued
    crawl.total_completed = total_completed
    crawl.total_failed = total_failed
    crawl.created_at = datetime.now(UTC)
    crawl.completed_at = None
    return crawl


def make_page(
    *,
    page_id: uuid.UUID | None = None,
    crawl_id: uuid.UUID | None = None,
    url: str = "https://example.com",
    depth: int = 0,
    status: str = "pending",
) -> MagicMock:
    """Return a mock object that behaves like a CrawlPage ORM row."""
    page = MagicMock()
    page.id = page_id or uuid.uuid4()
    page.crawl_id = crawl_id or uuid.uuid4()
    page.url = url
    page.depth = depth
    page.status = status
    page.result_path = None
    page.error = None
    page.created_at = datetime.now(UTC)
    page.completed_at = None
    return page


def make_queue_item(
    *,
    item_id: uuid.UUID | None = None,
    crawl_id: uuid.UUID | None = None,
    url: str = "https://example.com",
    depth: int = 0,
    status: str = "pending",
    crawl_page_id: uuid.UUID | None = None,
) -> MagicMock:
    """Return a mock object that behaves like a CrawlQueueItem ORM row."""
    item = MagicMock()
    item.id = item_id or uuid.uuid4()
    item.crawl_id = crawl_id or uuid.uuid4()
    item.url = url
    item.depth = depth
    item.status = status
    item.crawl_page_id = crawl_page_id
    item.created_at = datetime.now(UTC)
    item.dispatched_at = None
    item.completed_at = None
    return item


def make_mock_db(crawl=None, page=None) -> AsyncMock:
    """
    Build a mock async DB session.

    db.get(Crawl, id) returns `crawl`; db.get(CrawlPage, id) returns `page`.
    db.execute returns a MagicMock with rowcount=1 (insert succeeded).
    db.scalar returns 0 (no active queue items — used for completion checks).
    """
    from coordinator.models import Crawl, CrawlPage

    db = AsyncMock()

    async def _get(cls, id_val):
        if cls is Crawl:
            return crawl
        if cls is CrawlPage:
            return page
        return None

    db.get = AsyncMock(side_effect=_get)

    mock_exec_result = MagicMock()
    mock_exec_result.rowcount = 1
    mock_exec_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_exec_result)
    db.scalar = AsyncMock(return_value=0)
    db.add = MagicMock()  # synchronous — not awaited; suppress AsyncMock coroutine warning

    return db


def make_mock_minio(html_content: bytes = b"<html><body></body></html>") -> AsyncMock:
    """Build a mock MinIO client whose get_object returns `html_content`."""
    minio = AsyncMock()
    response = MagicMock()
    response.read = AsyncMock(return_value=html_content)
    response.close = MagicMock()
    minio.get_object = AsyncMock(return_value=response)
    return minio


def make_result_data(
    *,
    crawl_id: str,
    crawl_page_id: str,
    depth: int = 0,
    status: str = "completed",
    minio_path: str = "scrapeflow-results/history/abc/123.html",
    error: str | None = None,
) -> dict:
    """Build a worker result message dict with crawl_context present."""
    data: dict = {
        "job_id": crawl_page_id,
        "run_id": str(uuid.uuid4()),
        "status": status,
        "crawl_context": {
            "crawl_id": crawl_id,
            "crawl_page_id": crawl_page_id,
            "depth": depth,
        },
    }
    if status == "completed":
        data["minio_path"] = minio_path
    if error:
        data["error"] = error
    return data
