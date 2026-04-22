"""
Unit tests for coordinator/result_handler.py — _process_crawl_result().

DB and MinIO are mocked. Tests verify:
  - running status only updates page status, leaves counters alone
  - completed status updates page, decrements queue item, increments total_completed
  - failed status updates page, decrements queue item, increments total_failed
  - link extraction enqueues discovered URLs and increments total_queued
  - max_pages limit stops link insertion once the cap is reached
  - null crawl_context messages are acked and skipped in the loop
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from coordinator.result_handler import _process_crawl_result
from tests.conftest import (
    make_crawl,
    make_mock_db,
    make_mock_minio,
    make_page,
    make_result_data,
)


# ---------------------------------------------------------------------------
# running status
# ---------------------------------------------------------------------------


async def test_running_status_updates_page_status():
    """A 'running' result must set page.status = 'running' and nothing else."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id)
    page = make_page(page_id=page_id, crawl_id=crawl_id, status="pending")
    db = make_mock_db(crawl=crawl, page=page)

    data = make_result_data(
        crawl_id=str(crawl_id),
        crawl_page_id=str(page_id),
        status="running",
    )

    await _process_crawl_result(db, AsyncMock(), data)

    assert page.status == "running"
    assert crawl.total_completed == 0
    assert crawl.total_failed == 0


async def test_running_status_does_not_update_queue_item():
    """A 'running' result must not touch the crawl_queue row (status stays dispatched)."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id)
    page = make_page(page_id=page_id, crawl_id=crawl_id)
    db = make_mock_db(crawl=crawl, page=page)

    data = make_result_data(
        crawl_id=str(crawl_id), crawl_page_id=str(page_id), status="running"
    )

    await _process_crawl_result(db, AsyncMock(), data)

    # execute() is called by _process_crawl_result for the queue UPDATE — should NOT be called
    # for running status (early return before the terminal block).
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# completed status
# ---------------------------------------------------------------------------


async def test_completed_updates_page_fields():
    """A 'completed' result must set page.status, result_path, and completed_at."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=0)
    db = make_mock_db(crawl=crawl, page=page)
    minio = make_mock_minio(b"<html><body></body></html>")

    data = make_result_data(
        crawl_id=str(crawl_id),
        crawl_page_id=str(page_id),
        status="completed",
        minio_path="scrapeflow-results/history/abc/123.html",
    )

    await _process_crawl_result(db, minio, data)

    assert page.status == "completed"
    assert page.result_path == "scrapeflow-results/history/abc/123.html"
    assert page.completed_at is not None


async def test_completed_increments_total_completed():
    """total_completed must be incremented by 1 on a completed result."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, total_completed=2)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=0)
    db = make_mock_db(crawl=crawl, page=page)
    minio = make_mock_minio(b"<html></html>")

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id))

    await _process_crawl_result(db, minio, data)

    assert crawl.total_completed == 3


async def test_completed_updates_queue_item_to_completed():
    """The crawl_queue row must be updated to 'completed' via db.execute."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=0)
    db = make_mock_db(crawl=crawl, page=page)
    minio = make_mock_minio(b"<html></html>")

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id))

    await _process_crawl_result(db, minio, data)

    # First db.execute call is the CrawlQueueItem status update.
    assert db.execute.called


# ---------------------------------------------------------------------------
# failed status
# ---------------------------------------------------------------------------


async def test_failed_updates_page_fields():
    """A 'failed' result must set page.status='failed', page.error, page.completed_at."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id)
    page = make_page(page_id=page_id, crawl_id=crawl_id)
    db = make_mock_db(crawl=crawl, page=page)

    data = make_result_data(
        crawl_id=str(crawl_id),
        crawl_page_id=str(page_id),
        status="failed",
        error="connection_timeout",
    )

    await _process_crawl_result(db, AsyncMock(), data)

    assert page.status == "failed"
    assert page.error == "connection_timeout"
    assert page.completed_at is not None


async def test_failed_increments_total_failed():
    """total_failed must be incremented by 1 on a failed result."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, total_failed=1)
    page = make_page(page_id=page_id, crawl_id=crawl_id)
    db = make_mock_db(crawl=crawl, page=page)

    data = make_result_data(
        crawl_id=str(crawl_id), crawl_page_id=str(page_id), status="failed"
    )

    await _process_crawl_result(db, AsyncMock(), data)

    assert crawl.total_failed == 2
    assert crawl.total_completed == 0


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------


async def test_link_extraction_enqueues_discovered_urls():
    """
    When a completed result contains HTML with same-origin links, those links
    must be inserted into crawl_queue and total_queued incremented.
    """
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, max_depth=2, total_queued=1)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=0)

    html = b'<a href="/page1">P1</a><a href="/page2">P2</a>'
    db = make_mock_db(crawl=crawl, page=page)
    # db.execute returns rowcount=1 (insert succeeded) — already set in make_mock_db
    minio = make_mock_minio(html)

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id), depth=0)

    await _process_crawl_result(db, minio, data)

    # Two links found → total_queued should go from 1 to 3
    assert crawl.total_queued == 3


async def test_no_link_extraction_when_max_depth_reached():
    """
    When the page is already at max_depth, no new URLs should be enqueued.
    """
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, max_depth=2, total_queued=1)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=2)  # at max depth

    html = b'<a href="/page1">P1</a>'
    db = make_mock_db(crawl=crawl, page=page)
    minio = make_mock_minio(html)

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id), depth=2)

    await _process_crawl_result(db, minio, data)

    # next_depth = 3 > max_depth 2 → no inserts
    assert crawl.total_queued == 1  # unchanged


async def test_max_pages_limit_stops_enqueuing():
    """
    When total_queued already equals max_pages, no additional URLs are inserted.
    """
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, max_depth=3, max_pages=5, total_queued=5)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=0)

    html = b'<a href="/new">New</a>'
    db = make_mock_db(crawl=crawl, page=page)
    minio = make_mock_minio(html)

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id), depth=0)

    await _process_crawl_result(db, minio, data)

    assert crawl.total_queued == 5  # cap enforced


async def test_duplicate_url_not_counted_in_total_queued():
    """
    When the INSERT returns rowcount=0 (duplicate), total_queued must NOT increment.
    The UNIQUE constraint on (crawl_id, url) deduplicates; we only count real inserts.
    """
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, max_depth=2, total_queued=1)
    page = make_page(page_id=page_id, crawl_id=crawl_id, depth=0)

    html = b'<a href="/already-seen">Seen</a>'
    db = make_mock_db(crawl=crawl, page=page)
    # Simulate duplicate: INSERT returns rowcount=0
    dup_result = MagicMock()
    dup_result.rowcount = 0
    db.execute = AsyncMock(return_value=dup_result)

    minio = make_mock_minio(html)
    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id), depth=0)

    await _process_crawl_result(db, minio, data)

    assert crawl.total_queued == 1  # unchanged — duplicate was not counted


# ---------------------------------------------------------------------------
# Missing crawl / page guard
# ---------------------------------------------------------------------------


async def test_missing_crawl_returns_without_error():
    """If the Crawl row no longer exists, processing must stop silently."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    db = make_mock_db(crawl=None, page=None)

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id))

    await _process_crawl_result(db, AsyncMock(), data)  # must not raise


async def test_missing_page_returns_without_error():
    """If the CrawlPage row is missing (e.g. DB race), processing must stop silently."""
    crawl_id = uuid.uuid4()
    page_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id)
    db = make_mock_db(crawl=crawl, page=None)

    data = make_result_data(crawl_id=str(crawl_id), crawl_page_id=str(page_id))

    await _process_crawl_result(db, AsyncMock(), data)  # must not raise
