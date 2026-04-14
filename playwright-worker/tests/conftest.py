"""
Shared test fixtures for playwright-worker unit tests.

No live connections here — everything is mocked. The fixtures below
provide reusable mock objects; helper functions (make_nats_msg,
make_browser) live in test_main.py since they need per-test tuning.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_minio():
    """AsyncMock MinIO client — put_object is awaitable and records calls."""
    return AsyncMock()


def make_nats_msg(
    job_id: str = "job-aaa",
    run_id: str = "run-bbb",
    url: str = "https://example.com",
    output_format: str = "html",
    playwright_options: dict | None = None,
    stream_seq: int = 42,
) -> MagicMock:
    """
    Build a mock NATS message whose .data is a valid JobMessage JSON payload.
    msg.ack is an AsyncMock so tests can assert it was called.
    """
    payload: dict = {
        "job_id": job_id,
        "run_id": run_id,
        "url": url,
        "output_format": output_format,
    }
    if playwright_options is not None:
        payload["playwright_options"] = playwright_options

    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    msg.metadata = MagicMock()
    msg.metadata.sequence = MagicMock()
    msg.metadata.sequence.stream = stream_seq
    msg.ack = AsyncMock()
    return msg


def make_browser(
    html: str = "<html><head><title>Test Page</title></head><body><p>Hello</p></body></html>",
    page_url: str = "https://example.com/",
):
    """
    Build a mock Playwright browser / context / page stack.
    Returns (browser, context, page) so tests can assert on each level.

    page.content() returns `html`; page.url is the string `page_url`.
    All async methods are AsyncMock so they can be awaited and inspected.
    """
    page = AsyncMock()
    page.url = page_url  # property access, not a call — must be a plain string
    page.content = AsyncMock(return_value=html)

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    return browser, context, page
