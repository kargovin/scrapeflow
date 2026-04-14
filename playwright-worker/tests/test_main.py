"""
Unit tests for worker/main.py — _handle_message().

These tests verify the full ADR-002 job lifecycle without any live
infrastructure. NATS, MinIO, and the Playwright browser are all AsyncMocks.

The `upload` function is patched at 'worker.main.upload' (the name as it
appears in the module under test, not where it's defined in storage.py).
This is the standard unittest.mock rule for patching imported names.

Lifecycle summary being tested:
  1. Parse JobMessage from msg.data — ack+skip if malformed
  2. Publish status="running" with nats_stream_seq BEFORE page interaction
  3. (Optional) Set up image-blocking route on page
  4. page.goto → page.wait_for_load_state → page.content()
  5. format_output → upload to MinIO
  6. Publish status="completed" with minio_path
  7. msg.ack() — always, even on failure
  8. context.close() — always (finally block)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch


from tests.conftest import make_browser, make_nats_msg
from worker.main import RESULT_SUBJECT, _handle_message

_FAKE_MINIO_PATH = "scrapeflow-results/history/job-aaa/1234567890.html"
_DEFAULT_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Helper: run _handle_message with upload patched to return a fixed path
# ---------------------------------------------------------------------------


async def _run(msg, js=None, browser=None):
    """Run _handle_message with upload stubbed out; returns (js, mock_upload)."""
    js = js or AsyncMock()
    if browser is None:
        browser, _, _ = make_browser()
    with patch("worker.main.upload", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = _FAKE_MINIO_PATH
        await _handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)
    return js, mock_upload


# ---------------------------------------------------------------------------
# Malformed message
# ---------------------------------------------------------------------------


async def test_malformed_json_is_acked_immediately():
    """
    A message that cannot be parsed as JobMessage must be acked and dropped.
    Publishing and MinIO writes must NOT happen — there is nothing to process.
    """
    msg = MagicMock()
    msg.data = b"not valid json {{{"
    msg.ack = AsyncMock()
    js = AsyncMock()

    await _handle_message(msg, js, AsyncMock(), AsyncMock(), _DEFAULT_TIMEOUT)

    msg.ack.assert_called_once()
    js.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path — publish ordering
# ---------------------------------------------------------------------------


async def test_first_publish_is_running_with_stream_seq():
    """
    The very first NATS publish must be status='running' and carry
    nats_stream_seq from msg.metadata.sequence.stream (used by Step 22
    MaxDeliver advisory handler to identify stalled runs).
    """
    msg = make_nats_msg(stream_seq=77)
    js = AsyncMock()
    browser, _, _ = make_browser()

    js, _ = await _run(msg, js=js, browser=browser)

    first_call = js.publish.call_args_list[0]
    subject, payload_bytes = first_call.args
    data = json.loads(payload_bytes)

    assert subject == RESULT_SUBJECT
    assert data["status"] == "running"
    assert data["nats_stream_seq"] == 77
    assert "minio_path" not in data


async def test_second_publish_is_completed_with_minio_path():
    """
    After a successful MinIO upload, the second publish must be
    status='completed' and include the minio_path returned by upload().
    """
    msg = make_nats_msg()
    js = AsyncMock()
    browser, _, _ = make_browser()

    js, _ = await _run(msg, js=js, browser=browser)

    assert js.publish.call_count == 2
    second_call = js.publish.call_args_list[1]
    _, payload_bytes = second_call.args
    data = json.loads(payload_bytes)

    assert data["status"] == "completed"
    assert data["minio_path"] == _FAKE_MINIO_PATH
    assert "error" not in data


async def test_ack_called_once_on_success():
    """msg.ack() must be called exactly once after a successful job run."""
    msg = make_nats_msg()
    js, _ = await _run(msg)
    msg.ack.assert_called_once()


async def test_context_closed_on_success():
    """
    context.close() must be called after a successful run (the finally block).
    This prevents browser session state from leaking between jobs.
    """
    msg = make_nats_msg()
    browser, context, _ = make_browser()
    await _run(msg, browser=browser)
    context.close.assert_called_once()


# ---------------------------------------------------------------------------
# Failure path — page.goto raises
# ---------------------------------------------------------------------------


async def test_page_goto_failure_publishes_failed():
    """
    When page.goto raises, the worker must publish status='failed' with the
    error string — the API result consumer uses this to mark the run failed.
    """
    msg = make_nats_msg()
    js = AsyncMock()
    browser, _, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("connection timeout"))

    await _handle_message(msg, js, AsyncMock(), browser, _DEFAULT_TIMEOUT)

    last_call = js.publish.call_args_list[-1]
    _, payload_bytes = last_call.args
    data = json.loads(payload_bytes)
    assert data["status"] == "failed"
    assert data["error"] == "connection timeout"
    assert "minio_path" not in data


async def test_ack_called_on_failure():
    """
    msg.ack() must be called even when page.goto raises.
    Mirrors the Go worker: the API already knows the run failed via the result
    event — not acking would redeliver, but re-delivery won't fix a down site.
    """
    msg = make_nats_msg()
    browser, _, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("timeout"))

    await _handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    msg.ack.assert_called_once()


async def test_context_closed_on_failure():
    """context.close() must be called even when page.goto raises (finally block)."""
    msg = make_nats_msg()
    browser, context, page = make_browser()
    page.goto = AsyncMock(side_effect=Exception("timeout"))

    await _handle_message(msg, AsyncMock(), AsyncMock(), browser, _DEFAULT_TIMEOUT)

    context.close.assert_called_once()


# ---------------------------------------------------------------------------
# block_images option
# ---------------------------------------------------------------------------


async def test_block_images_true_calls_page_route():
    """
    When playwright_options.block_images=True, page.route() must be called
    once with a glob pattern that covers image and font extensions.
    """
    msg = make_nats_msg(
        playwright_options={
            "wait_strategy": "load",
            "timeout_seconds": 30,
            "block_images": True,
        }
    )
    browser, _, page = make_browser()

    await _run(msg, browser=browser)

    page.route.assert_called_once()
    route_pattern: str = page.route.call_args.args[0]
    # Pattern must cover at least common image types
    assert "png" in route_pattern
    assert "jpg" in route_pattern


async def test_block_images_false_does_not_call_page_route():
    """When block_images=False (default), page.route() must NOT be called."""
    msg = make_nats_msg(
        playwright_options={
            "wait_strategy": "load",
            "timeout_seconds": 30,
            "block_images": False,
        }
    )
    browser, _, page = make_browser()

    await _run(msg, browser=browser)

    page.route.assert_not_called()
