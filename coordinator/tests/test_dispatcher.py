"""
Unit tests for coordinator/dispatcher.py and completion helpers in result_handler.py.

DB and NATS are mocked — no live infrastructure required.

Key helpers under test:
  reenqueue_stalled(db)        — resets stale dispatched items to pending on startup
  check_completion(db, crawl)  — marks crawl completed when queue is empty
  _dispatch_batch(js)          — creates crawl_pages, publishes NATS, updates queue items
"""

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinator.constants import (
    NATS_JOBS_RUN_HTTP_SUBJECT as HTTP_SUBJECT,
    NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT as PLAYWRIGHT_SUBJECT,
)
from coordinator.dispatcher import _dispatch_batch, reenqueue_stalled
from coordinator.models import WebhookDelivery
from coordinator.result_handler import check_completion, enqueue_crawl_webhook
from tests.conftest import make_crawl, make_mock_db, make_queue_item


# ---------------------------------------------------------------------------
# reenqueue_stalled
# ---------------------------------------------------------------------------


async def test_reenqueue_stalled_calls_update():
    """reenqueue_stalled must execute an UPDATE query against crawl_queue."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 2
    db.execute = AsyncMock(return_value=mock_result)

    await reenqueue_stalled(db)

    db.execute.assert_called_once()


async def test_reenqueue_stalled_zero_rows_no_error():
    """reenqueue_stalled must not raise even when no rows match."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 0
    db.execute = AsyncMock(return_value=mock_result)

    await reenqueue_stalled(db)  # should not raise


# ---------------------------------------------------------------------------
# check_completion
# ---------------------------------------------------------------------------


async def test_check_completion_marks_crawl_when_queue_empty():
    """
    When db.scalar returns 0 (no active items), the crawl must be set to
    'completed' with a completed_at timestamp, and True returned.
    """
    crawl = make_crawl(status="running", total_completed=3, total_failed=0)
    db = make_mock_db(crawl=crawl)
    db.scalar = AsyncMock(return_value=0)

    result = await check_completion(db, crawl)

    assert result is True
    assert crawl.status == "completed"
    assert crawl.completed_at is not None


async def test_check_completion_no_action_when_items_remain():
    """
    When db.scalar returns > 0 (active items still in queue), the crawl status
    must not change and False must be returned.
    """
    crawl = make_crawl(status="running")
    db = make_mock_db(crawl=crawl)
    db.scalar = AsyncMock(return_value=5)

    result = await check_completion(db, crawl)

    assert result is False
    assert crawl.status == "running"
    assert crawl.completed_at is None


# ---------------------------------------------------------------------------
# _dispatch_batch — NATS publish routing
# ---------------------------------------------------------------------------


def _make_session_ctx(db):
    """Return a callable that always yields `db` as an async context manager."""

    @asynccontextmanager
    async def _ctx():
        yield db

    return _ctx


async def test_dispatch_batch_publishes_to_http_subject_for_http_engine():
    """
    A pending queue item for an http-engine crawl must publish to
    scrapeflow.jobs.run.http, not the playwright subject.
    """
    crawl_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, status="running", engine="http")
    item = make_queue_item(crawl_id=crawl_id, url="https://example.com/page", depth=1)

    db = AsyncMock()
    db.get = AsyncMock(return_value=crawl)
    db.add = MagicMock()
    mock_exec = MagicMock()
    mock_exec.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(return_value=mock_exec)

    js = AsyncMock()

    with patch("coordinator.dispatcher.AsyncSessionLocal", _make_session_ctx(db)):
        await _dispatch_batch(js)

    js.publish.assert_called_once()
    subject = js.publish.call_args.args[0]
    assert subject == HTTP_SUBJECT


async def test_dispatch_batch_publishes_to_playwright_subject_for_playwright_engine():
    """A playwright-engine crawl must publish to scrapeflow.jobs.run.playwright."""
    crawl_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, status="running", engine="playwright")
    item = make_queue_item(crawl_id=crawl_id, url="https://example.com/page", depth=1)

    db = AsyncMock()
    db.get = AsyncMock(return_value=crawl)
    db.add = MagicMock()
    mock_exec = MagicMock()
    mock_exec.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(return_value=mock_exec)

    js = AsyncMock()

    with patch("coordinator.dispatcher.AsyncSessionLocal", _make_session_ctx(db)):
        await _dispatch_batch(js)

    subject = js.publish.call_args.args[0]
    assert subject == PLAYWRIGHT_SUBJECT


async def test_dispatch_batch_payload_contains_crawl_context():
    """
    The dispatched NATS payload must include a crawl_context sub-object with
    crawl_id, crawl_page_id, and depth — workers need this for result routing.
    """
    crawl_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, status="running", engine="http")
    item = make_queue_item(crawl_id=crawl_id, url="https://example.com/p", depth=2)

    db = AsyncMock()
    db.get = AsyncMock(return_value=crawl)
    db.add = MagicMock()
    mock_exec = MagicMock()
    mock_exec.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(return_value=mock_exec)

    js = AsyncMock()

    with patch("coordinator.dispatcher.AsyncSessionLocal", _make_session_ctx(db)):
        await _dispatch_batch(js)

    _, payload_bytes = js.publish.call_args.args
    payload = json.loads(payload_bytes)

    assert "crawl_context" in payload
    assert payload["crawl_context"]["crawl_id"] == str(crawl_id)
    assert payload["crawl_context"]["depth"] == 2
    assert payload["output_format"] == "html"
    assert payload["schema_version"] == 2


async def test_dispatch_batch_skips_cancelled_crawl():
    """
    A pending queue item whose crawl is 'cancelled' must be set to 'skipped'.
    No NATS message must be published.
    """
    crawl_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, status="cancelled")
    item = make_queue_item(crawl_id=crawl_id)

    db = AsyncMock()
    db.get = AsyncMock(return_value=crawl)
    mock_exec = MagicMock()
    mock_exec.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(return_value=mock_exec)

    js = AsyncMock()

    with patch("coordinator.dispatcher.AsyncSessionLocal", _make_session_ctx(db)):
        await _dispatch_batch(js)

    js.publish.assert_not_called()
    assert item.status == "skipped"


async def test_dispatch_batch_transitions_queued_to_running():
    """
    On the first dispatch for a 'queued' crawl, the crawl status must change
    to 'running' and total_queued must be set to 1.
    """
    crawl_id = uuid.uuid4()
    crawl = make_crawl(crawl_id=crawl_id, status="queued", total_queued=0)
    item = make_queue_item(crawl_id=crawl_id, url="https://example.com", depth=0)

    db = AsyncMock()
    db.get = AsyncMock(return_value=crawl)
    db.add = MagicMock()
    mock_exec = MagicMock()
    mock_exec.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(return_value=mock_exec)

    js = AsyncMock()

    with patch("coordinator.dispatcher.AsyncSessionLocal", _make_session_ctx(db)):
        await _dispatch_batch(js)

    assert crawl.status == "running"
    assert crawl.total_queued == 1


async def test_dispatch_batch_no_publish_when_queue_empty():
    """When there are no pending items, js.publish must not be called."""
    db = AsyncMock()
    mock_exec = MagicMock()
    mock_exec.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_exec)
    db.scalar = AsyncMock(return_value=0)

    js = AsyncMock()

    with patch("coordinator.dispatcher.AsyncSessionLocal", _make_session_ctx(db)):
        await _dispatch_batch(js)

    js.publish.assert_not_called()


# ---------------------------------------------------------------------------
# enqueue_crawl_webhook
# ---------------------------------------------------------------------------


def test_enqueue_crawl_webhook_adds_row_with_correct_fields():
    """
    When webhook_url is set, db.add must be called once with a WebhookDelivery
    whose crawl_id matches the crawl and whose payload carries the completion fields.
    crawl_id is critical — omitting it violates the DB CHECK constraint.
    """
    crawl = make_crawl(webhook_url="https://hooks.example.com/crawl", total_completed=5, total_failed=1)
    crawl.status = "completed"
    db = make_mock_db(crawl=crawl)

    enqueue_crawl_webhook(db, crawl)

    db.add.assert_called_once()
    row = db.add.call_args.args[0]
    assert isinstance(row, WebhookDelivery)
    assert row.crawl_id == crawl.id
    assert row.webhook_url == "https://hooks.example.com/crawl"
    assert row.status == "pending"
    assert row.payload["event"] == "crawl.completed"
    assert row.payload["crawl_id"] == str(crawl.id)
    assert row.payload["total_completed"] == 5
    assert row.payload["total_failed"] == 1


def test_enqueue_crawl_webhook_no_op_when_webhook_url_is_none():
    """When webhook_url is None, db.add must not be called — no row inserted."""
    crawl = make_crawl(webhook_url=None)
    db = make_mock_db(crawl=crawl)

    enqueue_crawl_webhook(db, crawl)

    db.add.assert_not_called()
